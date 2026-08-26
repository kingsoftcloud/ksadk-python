"""MemoryProviderResolver —— 根据 providerRef 解析真实 Memory Provider（方案 §2）。

providerRef 值映射：
  "local-default" → 持久 SQLite（resolve_default_memory_provider）
  "local-sqlite"  → 同上
  "local-inmemory" → InMemoryLTMBackend（测试用）
  "http"          → HttpLTMBackend（需 KSADK_LTM_HTTP_URL/TOKEN）
  "sdk"           → SdkLTMBackend（需 AK/SK + namespace）
  "longterm-service" → LongTermMemoryService.from_env()
"""

from __future__ import annotations

from typing import Protocol


class MemoryProviderLike(Protocol):
    def search_memory(self, user_id: str, query: str, top_k: int = 5, **kwargs) -> list[str]: ...
    def save_memory(self, user_id: str, event_strings: list[str], **kwargs) -> bool: ...


def resolve_memory_provider(provider_ref: str) -> MemoryProviderLike:
    """根据 providerRef 解析真实 Memory Provider。

    providerRef 值映射：
      "local-default" / "local-sqlite" → 持久 SQLite
      "local-inmemory" → InMemoryLTMBackend（测试用）
      "http" → HttpLTMBackend（需 KSADK_LTM_HTTP_URL/TOKEN）
      "sdk" → SdkLTMBackend（需 AK/SK + namespace）
      "longterm-service" → LongTermMemoryService.from_env()
      其他 → fallback 到持久 SQLite（兼容旧 AgentVersion）
    """
    ref = str(provider_ref or "").strip().lower()

    if ref in ("local-inmemory", "inmemory"):
        from ksadk.memory.adk.backends.inmemory_ltm_backend import (
            InMemoryLTMBackend,
        )

        return InMemoryLTMBackend()

    if ref in ("http",):
        import os

        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

        return HttpLTMBackend(
            index="ksadk",
            base_url=os.environ.get("KSADK_LTM_HTTP_URL", ""),
            token=os.environ.get("KSADK_LTM_HTTP_TOKEN", ""),
        )

    if ref in ("sdk",):
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

        return SdkLTMBackend(index="ksadk")

    if ref in ("longterm-service",):
        from ksadk.memory.service import LongTermMemoryService

        return LongTermMemoryService.from_env()

    # 默认：持久 SQLite（local-default / local-sqlite / 未知 ref）
    from ksadk.memory.providers.local_sqlite import (
        resolve_default_memory_provider,
    )

    return resolve_default_memory_provider()


__all__ = ["MemoryProviderLike", "resolve_memory_provider"]
