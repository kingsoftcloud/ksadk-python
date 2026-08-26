"""Shared long-term memory backend resolution."""

from __future__ import annotations


def get_long_term_memory_backend_cls(backend: str) -> type:
    if backend == "local":
        # 优先用持久 SQLite（解决 InMemory 进程退出后数据丢失、recall 和 flush 不同库）。
        # 设 KSADK_LTM_BACKEND=inmemory 可显式回退。
        import os

        if str(os.environ.get("KSADK_LTM_FORCE_INMEMORY", "")).strip().lower() in (
            "1",
            "true",
        ):
            from ksadk.memory.adk.backends.inmemory_ltm_backend import (
                InMemoryLTMBackend,
            )

            return InMemoryLTMBackend
        from ksadk.memory.adk.backends.sqlite_ltm_backend import SqliteLTMBackend

        return SqliteLTMBackend

    if backend == "http":
        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

        return HttpLTMBackend

    if backend == "sdk":
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

        return SdkLTMBackend

    raise ValueError(
        f"Unsupported long term memory backend: {backend}. Available: local, http, sdk"
    )
