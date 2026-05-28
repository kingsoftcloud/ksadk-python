"""Shared long-term memory backend resolution."""

from __future__ import annotations


def get_long_term_memory_backend_cls(backend: str) -> type:
    if backend == "local":
        from ksadk.memory.adk.backends.inmemory_ltm_backend import InMemoryLTMBackend

        return InMemoryLTMBackend

    if backend == "http":
        from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend

        return HttpLTMBackend

    if backend == "sdk":
        from ksadk.memory.adk.backends.sdk_ltm_backend import SdkLTMBackend

        return SdkLTMBackend

    raise ValueError(
        f"Unsupported long term memory backend: {backend}. Available: local, http, sdk"
    )
