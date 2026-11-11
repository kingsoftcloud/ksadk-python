"""Lazy public API for outbound MCP tunnels.

Keeping exports lazy allows runtime configuration modules to be imported without
loading Google ADK or enabling Tunnel itself.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "LocalServer": "ksadk.tunnel.connector",
    "TunnelConnector": "ksadk.tunnel.connector",
    "ServerDescriptor": "ksadk.tunnel.registry",
    "TunnelRegistry": "ksadk.tunnel.registry",
    "get_registry": "ksadk.tunnel.registry",
    "mount_tunnel": "ksadk.tunnel.server",
    "TunnelToolset": "ksadk.tunnel.toolset",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
