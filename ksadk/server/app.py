"""Explicit RuntimeAdapter-first FastAPI composition API.

There is deliberately no module-level ``app`` and no ``set_runner`` global.
Every caller must provide a RuntimeExecutor and RuntimeLaunchContext through
RuntimeAppConfig, so Web, protocol, harness, and generated entrypoints share the
same execution contract.
"""

from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import (
    ALL_GROUPS,
    CONTROL_PLANE_GROUPS,
    DATA_PLANE_GROUPS,
    RuntimeAppConfig,
    RuntimeAppState,
    create_runtime_app,
)

__all__ = [
    "ALL_GROUPS",
    "CONTROL_PLANE_GROUPS",
    "DATA_PLANE_GROUPS",
    "RuntimeAppConfig",
    "RuntimeAppState",
    "configure_runtime_app",
    "create_runtime_app",
]
