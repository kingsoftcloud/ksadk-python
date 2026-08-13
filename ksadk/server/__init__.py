"""Lazy RuntimeAdapter-first server composition exports.

Keeping package import side-effect free is required because conversation models
import ``ksadk.server.api_models`` during RuntimeAdapter initialization.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ALL_GROUPS",
    "CONTROL_PLANE_GROUPS",
    "DATA_PLANE_GROUPS",
    "RuntimeAppConfig",
    "RuntimeAppState",
    "configure_runtime_app",
    "create_runtime_app",
]


def __getattr__(name: str) -> Any:
    if name == "routes":
        value = importlib.import_module("ksadk.server.routes")
        globals()[name] = value
        return value
    if name == "configure_runtime_app":
        from ksadk.server.composition import configure_runtime_app

        return configure_runtime_app
    if name in {
        "ALL_GROUPS",
        "CONTROL_PLANE_GROUPS",
        "DATA_PLANE_GROUPS",
        "RuntimeAppConfig",
        "RuntimeAppState",
        "create_runtime_app",
    }:
        factory = importlib.import_module("ksadk.server.factory")
        return getattr(factory, name)
    raise AttributeError(name)
