"""Lazy public API for outbound MCP tunnels.

Keeping exports lazy allows runtime configuration modules to be imported without
loading Google ADK or enabling Tunnel itself.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "LocalMCPService": "ksadk.tunnel.services",
    "LocalAPIService": "ksadk.tunnel.services",
    "APIOperation": "ksadk.tunnel.services",
    "TunnelConnector": "ksadk.tunnel.connector",
    "TunnelError": "ksadk.tunnel.protocol",
    "TunnelLimits": "ksadk.tunnel.protocol",
    "TunnelRegistry": "ksadk.tunnel.registry",
    "TunnelToolset": "ksadk.tunnel.toolset",
    "TunnelTools": "ksadk.tunnel.langgraph",
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
