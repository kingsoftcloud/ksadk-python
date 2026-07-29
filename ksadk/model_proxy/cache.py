"""能力探测缓存(v2.4):租户隔离键 + TTL + singleflight + 主动失效。

review v1 指出 ``(model, base_url)`` 缺 credential/tenant 维度:同一网关不同 key
能力可能不同(如星流 glm-5.1 的 responses 403 按 consumer 鉴权),404 也可能是
"模型不存在"而非"endpoint 不存在"。本模块:

- 键 = ``(model, base_url, credential_scope)``;credential_scope 用进程内 HMAC
  标识符，避免明文 key 入键/日志，又能区分不同 key。
- **明确判定**(supported/unsupported)长缓存(TTL);**不确定**(unknown)不缓存,
  下次重探——避免把一次抖动固化成长期误判。
- **singleflight**:并发同键只探一次,其余等结果(用 per-key Future)。
- **主动失效**:`invalidate(model, base_url, scope)` 与 `clear()` 入口。
- 撞名/模型不存在的结构化区分由 detect.py 的错误分类负责,本缓存只存结论。
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .detect import ModelCapabilities

# The capability cache is process-local, so its credential namespace need not
# survive a restart.  A random key prevents cache identifiers from being used as
# an offline oracle for API keys and avoids retaining the original credential.
_SCOPE_HMAC_KEY = secrets.token_bytes(32)


def _scope(key: str) -> str:
    """Return a process-local, non-secret cache scope for a credential."""
    return hmac.digest(_SCOPE_HMAC_KEY, key.encode("utf-8"), "sha256").hex()[:16]


@dataclass
class _Entry:
    caps: ModelCapabilities
    expires_at: float  # 0 = 永不过期(明确判定)


class CapabilityCache:
    """能力矩阵缓存:线程安全 + asyncio 安全(单进程内)。"""

    def __init__(self, ttl: float = 3600.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        # singleflight:sync per-key Event,async per-key Future
        self._inflight_sync: dict[tuple[str, str, str], threading.Event] = {}
        self._inflight_async: dict[tuple[str, str, str], asyncio.Future] = {}

    def _key(self, model: str, base: str, key: str) -> tuple[str, str, str]:
        return (model, base.rstrip("/"), _scope(key))

    def get(self, model: str, base: str, key: str) -> ModelCapabilities | None:
        """命中且未过期返回 caps;过期/不存在/不确定(不缓存)返回 None。"""
        k = self._key(model, base, key)
        with self._lock:
            e = self._entries.get(k)
            if e is None:
                return None
            if e.expires_at and time.time() > e.expires_at:
                self._entries.pop(k, None)
                return None
            return e.caps

    def put(self, model: str, base: str, key: str, caps: ModelCapabilities) -> None:
        """只缓存明确判定(supported/unsupported);unknown 不缓存。"""
        if caps.verdict == "unknown":
            return
        k = self._key(model, base, key)
        expires = time.time() + self._ttl if self._ttl > 0 else 0
        with self._lock:
            self._entries[k] = _Entry(caps=caps, expires_at=expires)

    def invalidate(self, model: str, base: str, key: str) -> None:
        k = self._key(model, base, key)
        with self._lock:
            self._entries.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ---- singleflight:并发同键只探一次 ----
    def get_or_probe(
        self,
        model: str,
        base: str,
        key: str,
        probe: Callable[[str, str, str], ModelCapabilities],
    ) -> ModelCapabilities:
        """命中直接返回;否则 singleflight 探测(并发同键只探一次),结果入缓存。

        probe 是同步 callable(model, base, key) -> ModelCapabilities。
        """
        cached = self.get(model, base, key)
        if cached is not None:
            return cached
        k = self._key(model, base, key)
        with self._lock:
            ev = self._inflight_sync.get(k)
            if ev is not None:
                # 已有并发探测在进行:等它完成,读缓存(探测方负责 put)
                pass
            else:
                ev = threading.Event()
                self._inflight_sync[k] = ev
                ev = None  # 标记本线程负责探测
        if ev is not None:
            ev.wait(timeout=30.0)
            return self.get(model, base, key) or ModelCapabilities(verdict="unknown")
        # 本线程负责探测
        try:
            caps = probe(model, base, key)
            self.put(model, base, key, caps)
            return caps
        finally:
            with self._lock:
                ev_done = self._inflight_sync.pop(k, None)
            if ev_done is not None:
                ev_done.set()

    async def aget_or_probe(
        self,
        model: str,
        base: str,
        key: str,
        probe: Callable[[str, str, str], Any],
    ) -> ModelCapabilities:
        """async singleflight 版:probe 是 async callable -> ModelCapabilities。"""
        cached = self.get(model, base, key)
        if cached is not None:
            return cached
        k = self._key(model, base, key)
        loop = asyncio.get_running_loop()
        with self._lock:
            fut = self._inflight_async.get(k)
            if fut is None:
                fut = loop.create_future()
                self._inflight_async[k] = fut
                own = True
            else:
                own = False
        if not own:
            await fut  # type: ignore[no-any-return]
            # owner 已 put 入缓存;从缓存读结果(不用 fut 的值,它是 None 占位)
            return self.get(model, base, key) or ModelCapabilities(verdict="unknown")
        try:
            caps: ModelCapabilities = await probe(model, base, key)
            self.put(model, base, key, caps)
            return caps
        finally:
            with self._lock:
                self._inflight_async.pop(k, None)
            if not fut.done():
                fut.set_result(None)  # 唤醒等待者(它们会读缓存)
