from __future__ import annotations

import atexit
import contextvars
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from ksadk.sandbox.base import SandboxBackend, SandboxSession

logger = logging.getLogger(__name__)

# 后台 sweep 默认间隔(秒)。0 表示禁用后台 sweep,仅靠 get_or_create 时的惰性 sweep。
_DEFAULT_SWEEP_INTERVAL_SECONDS = 60


@dataclass
class SandboxRegistryEntry:
    key: str
    backend: str
    session: SandboxSession
    created_at: float
    last_used_at: float
    expires_at: float
    isolated: bool

    @property
    def sandbox_id(self) -> str:
        return self.session.sandbox_id


class SandboxRegistry:
    def __init__(self):
        self._entries: dict[str, SandboxRegistryEntry] = {}
        # RLock 可重入:get_or_create 内部会调 sweep/kill/_enforce_quota。
        self._lock = threading.RLock()
        # idle_ttl 由 toolset 层在 get_or_create 时传入,后台 sweep 复用该值。
        self._idle_ttl_seconds: int = 0
        self._sweep_thread: threading.Thread | None = None
        self._sweep_stop = threading.Event()
        self._sweep_interval = self._resolve_sweep_interval()

    @staticmethod
    def _resolve_sweep_interval() -> int:
        raw = os.environ.get(
            "KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", str(_DEFAULT_SWEEP_INTERVAL_SECONDS)
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_SWEEP_INTERVAL_SECONDS
        return value if value > 0 else 0

    def get_or_create(
        self,
        *,
        key: str,
        backend_name: str,
        backend: SandboxBackend,
        ttl_seconds: int,
        isolated: bool,
        idle_ttl_seconds: int | None = None,
        max_sessions: int | None = None,
        input_files: list | None = None,
        now: float | None = None,
    ) -> tuple[SandboxRegistryEntry, bool]:
        current = time.time() if now is None else now
        if idle_ttl_seconds is not None:
            self._idle_ttl_seconds = int(idle_ttl_seconds or 0)
        with self._lock:
            self.sweep(now=current, idle_ttl_seconds=self._idle_ttl_seconds)
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > current:
                entry.last_used_at = current
                return entry, False
            if entry is not None:
                self.kill(key)
            self._enforce_quota(max_sessions=max_sessions, keep_key=key)
            session = backend.create_session(session_id=key, input_files=input_files or None)
            entry = SandboxRegistryEntry(
                key=key,
                backend=backend_name,
                session=session,
                created_at=current,
                last_used_at=current,
                expires_at=current + max(1, int(ttl_seconds or 900)),
                isolated=isolated,
            )
            self._entries[key] = entry
        # 锁外启动后台线程,避免持锁创建线程。
        self._start_sweep_thread()
        return entry, True

    def latest(self) -> SandboxRegistryEntry | None:
        with self._lock:
            if not self._entries:
                return None
            return max(self._entries.values(), key=lambda item: item.last_used_at)

    def sweep(self, *, now: float | None = None, idle_ttl_seconds: int | None = None) -> int:
        current = time.time() if now is None else now
        idle_ttl = int(idle_ttl_seconds or 0)
        with self._lock:
            expired = [
                key
                for key, entry in self._entries.items()
                if entry.expires_at <= current
                or (idle_ttl > 0 and current - entry.last_used_at > idle_ttl)
            ]
        for key in expired:
            self.kill(key)
        return len(expired)

    def entries(self) -> list[SandboxRegistryEntry]:
        with self._lock:
            return list(self._entries.values())

    def kill(self, key: str) -> bool:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is None:
            return False
        # session.kill() 是 E2B 网络 IO,放锁外避免持锁阻塞并发 get_or_create。
        try:
            entry.session.kill()
        except Exception:
            logger.exception("sandbox session kill failed for key=%s", key)
        return True

    def clear(self) -> None:
        with self._lock:
            keys = list(self._entries)
        for key in keys:
            self.kill(key)

    def _enforce_quota(self, *, max_sessions: int | None, keep_key: str) -> None:
        if not max_sessions or max_sessions < 1:
            return
        to_kill: list[str] = []
        with self._lock:
            while len(self._entries) - len(to_kill) >= max_sessions:
                candidates = [
                    entry
                    for entry in self._entries.values()
                    if entry.key != keep_key and entry.key not in to_kill
                ]
                if not candidates:
                    break
                oldest = min(candidates, key=lambda item: item.last_used_at)
                to_kill.append(oldest.key)
        for key in to_kill:
            self.kill(key)

    def _start_sweep_thread(self) -> None:
        if self._sweep_interval <= 0:
            return
        with self._lock:
            if self._sweep_thread is not None and self._sweep_thread.is_alive():
                return
            self._sweep_stop.clear()
            self._sweep_thread = threading.Thread(
                target=self._sweep_loop,
                name="ksadk-sandbox-sweep",
                daemon=True,
            )
            self._sweep_thread.start()

    def _sweep_loop(self) -> None:
        while not self._sweep_stop.wait(timeout=self._sweep_interval):
            try:
                self.sweep(idle_ttl_seconds=self._idle_ttl_seconds)
            except Exception:
                logger.exception("sandbox background sweep failed")

    def reset_for_tests(self) -> None:
        """停后台 sweep 线程并清空所有 entry,用于测试隔离。"""
        self.close()
        self._idle_ttl_seconds = 0
        self._sweep_stop.clear()

    def close(self) -> None:
        """Stop the owned sweep thread and kill every sandbox session."""
        self._sweep_stop.set()
        thread = self._sweep_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._sweep_thread = None
        self.clear()


_current_sandbox_registry: contextvars.ContextVar[SandboxRegistry | None] = contextvars.ContextVar(
    "ksadk_sandbox_registry", default=None
)
_fallback_sandbox_registry = SandboxRegistry()


def set_fallback_sandbox_registry(registry: SandboxRegistry) -> None:
    """Set the registry used by legacy calls outside a bound app context."""
    global _fallback_sandbox_registry
    _fallback_sandbox_registry = registry


@contextmanager
def bind_sandbox_registry(registry: SandboxRegistry) -> Iterator[None]:
    token = _current_sandbox_registry.set(registry)
    try:
        yield
    finally:
        _current_sandbox_registry.reset(token)


def get_sandbox_registry() -> SandboxRegistry:
    return _current_sandbox_registry.get() or _fallback_sandbox_registry


class _ContextualSandboxRegistry:
    """Compatibility proxy resolving the registry owned by the current app."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_sandbox_registry(), name)

    def clear(self) -> None:
        # Keep atexit dynamic: resolve the default app registry when invoked,
        # not when the callback is registered.
        get_sandbox_registry().clear()

    def close(self) -> None:
        get_sandbox_registry().close()


GLOBAL_SANDBOX_REGISTRY = _ContextualSandboxRegistry()
# 进程退出时清理 sandbox,避免 E2B sandbox 活到服务端 timeout 才销毁(按秒计费)。
# clear() 幂等,与 server lifespan shutdown 重复调用安全。kill -9 不触发 atexit,只能靠 E2B 兜底。
atexit.register(GLOBAL_SANDBOX_REGISTRY.clear)
