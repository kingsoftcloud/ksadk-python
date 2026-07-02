from __future__ import annotations

import time
from dataclasses import dataclass

from ksadk.sandbox.base import SandboxBackend, SandboxSession


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
        self.sweep(now=current, idle_ttl_seconds=idle_ttl_seconds)
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
        return entry, True

    def latest(self) -> SandboxRegistryEntry | None:
        if not self._entries:
            return None
        return max(self._entries.values(), key=lambda item: item.last_used_at)

    def sweep(self, *, now: float | None = None, idle_ttl_seconds: int | None = None) -> int:
        current = time.time() if now is None else now
        idle_ttl = int(idle_ttl_seconds or 0)
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at <= current or (idle_ttl > 0 and current - entry.last_used_at > idle_ttl)
        ]
        for key in expired:
            self.kill(key)
        return len(expired)

    def entries(self) -> list[SandboxRegistryEntry]:
        return list(self._entries.values())

    def kill(self, key: str) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        try:
            entry.session.kill()
        except Exception:
            pass
        return True

    def clear(self) -> None:
        for key in list(self._entries):
            self.kill(key)

    def _enforce_quota(self, *, max_sessions: int | None, keep_key: str) -> None:
        if not max_sessions or max_sessions < 1:
            return
        while len(self._entries) >= max_sessions:
            candidates = [entry for entry in self._entries.values() if entry.key != keep_key]
            if not candidates:
                break
            oldest = min(candidates, key=lambda item: item.last_used_at)
            self.kill(oldest.key)


GLOBAL_SANDBOX_REGISTRY = SandboxRegistry()
