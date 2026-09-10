"""Independent callback registry to avoid __main__ / canonical module double-instance.

When the MCP server is launched via ``python -m ksadk.skills.mcp_server.server``,
Python creates two module instances: ``__main__`` (entry point) and the
canonical ``ksadk.skills.mcp_server.server``.  A module-level list in
``server.py`` would exist in both copies, and callbacks registered from the
canonical import would never be seen by the background thread running in
``__main__``.

This module is always imported as ``ksadk.skills.mcp_server._registry``
(no ``__main__`` ambiguity), so both code paths share the same list.
"""

from __future__ import annotations

from typing import Any, Callable

_refresh_callbacks: list[Callable[[str, list[Any]], None]] = []


def register_refresh_callback(callback: Callable[[str, list[Any]], None]) -> None:
    """Register a callback invoked when the skill manifest is refreshed."""
    if callback not in _refresh_callbacks:
        _refresh_callbacks.append(callback)


def get_refresh_callbacks() -> list[Callable[[str, list[Any]], None]]:
    """Return a snapshot of registered callbacks."""
    return list(_refresh_callbacks)


def clear_refresh_callbacks() -> None:
    _refresh_callbacks.clear()
