"""Framework-native adapters for canonical RuntimeEvent schema v2.

ADK adapter 依赖可选 extra ``ksadk[adk]``(google-adk)。托管 Codex 镜像只装
默认依赖,这里必须惰性导出,否则 ``ksadk.events.adapters`` 的传递 import 会
让 codex-only 环境在启动期直接崩溃。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ksadk.events.adapters.adk import ADKAdapterContext, ADKEventAdapter

__all__ = ["ADKAdapterContext", "ADKEventAdapter"]

_LAZY_ATTRS = {"ADKAdapterContext", "ADKEventAdapter"}


def __getattr__(name: str) -> Any:
    if name in _LAZY_ATTRS:
        try:
            from ksadk.events.adapters import adk as _adk
        except ModuleNotFoundError as exc:  # google.adk missing -> optional extra
            raise ImportError(
                "ADK event adapter requires the optional 'adk' extra "
                "(pip install ksadk[adk])"
            ) from exc
        return getattr(_adk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
