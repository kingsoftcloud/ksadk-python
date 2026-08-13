"""RuntimeAdapter Factory 的启动上下文与可注入依赖。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

_RUNTIME_TYPE_ALIASES = {
    # LangChain and DeepAgents are source-framework variants.  They share the
    # LangGraph execution/checkpoint contract and must not create new top-level
    # runtime types in the RuntimeAdapter registry.
    "langchain": "langgraph",
    "deepagents": "langgraph",
}


@dataclass(frozen=True)
class RuntimeServices:
    """创建 RuntimeAdapter 时可替换的进程级依赖。"""

    codex_client_factory: Callable[[], Any] | None = None
    runner_factory: Callable[[Any, str], Any] | None = None


@dataclass(frozen=True)
class RuntimeLaunchContext:
    """一次 RuntimeAdapter 创建所需的框架无关输入。"""

    runtime_type: str
    project_dir: Path
    detection: Any | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    services: RuntimeServices = field(default_factory=RuntimeServices)

    def __post_init__(self) -> None:
        runtime_type = str(self.runtime_type or "").strip().lower()
        canonical_runtime_type = _RUNTIME_TYPE_ALIASES.get(
            runtime_type, runtime_type
        )
        object.__setattr__(self, "runtime_type", canonical_runtime_type)
        object.__setattr__(self, "project_dir", Path(self.project_dir))
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))


__all__ = ["RuntimeLaunchContext", "RuntimeServices"]
