"""Memory backend provider registry."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest


class RenderResult(BaseModel):
    """Result from rendering a memory backend config."""

    backend_type: str
    config_patch: dict[str, Any] = Field(default_factory=dict)
    required_env: list[str] = Field(default_factory=list)
    plugin_ids: list[str] = Field(default_factory=list)
    disabled_plugin_ids: list[str] = Field(default_factory=list)
    clear_plugin_slots: list[str] = Field(default_factory=list)


class ProviderProtocol(Protocol):
    """Protocol for memory backend providers."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult: ...


_PROVIDERS: dict[str, ProviderProtocol] = {}


def register_provider(backend_type: str, provider: ProviderProtocol) -> None:
    """Register a memory backend provider."""
    _PROVIDERS[backend_type] = provider


def get_provider(backend_type: str) -> ProviderProtocol | None:
    """Get a registered provider by backend type."""
    return _PROVIDERS.get(backend_type)


def list_providers() -> list[str]:
    """List registered backend type identifiers."""
    return list(_PROVIDERS.keys())


def _register_builtin_providers() -> None:
    """Register providers shipped in this repo copy."""
    from ksadk_runtime_common.memory_backend.providers.mem0 import Mem0Provider
    from ksadk_runtime_common.memory_backend.providers.lancedb import LanceDBProvider

    register_provider("mem0", Mem0Provider())
    register_provider("lancedb", LanceDBProvider())


_register_builtin_providers()
