"""RuntimeAdapter Factory 的启动上下文与可注入依赖。"""

from __future__ import annotations

import os
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
    """一次 RuntimeAdapter 创建所需的框架无关输入。

    ``deployment_mode`` 与 ``runtime_type``/Context ownership 正交（方案 §4.3 / §6.1）：描述
    实例在哪里运行、谁负责构建/扩缩容/运维，不描述谁拥有最终模型输入。默认 ``local``，保持
    既有调用方行为不变；云端 Runtime 由控制面显式传入 ``ksadk_managed_cloud``/``external_managed``。
    """

    runtime_type: str
    project_dir: Path
    detection: Any | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    services: RuntimeServices = field(default_factory=RuntimeServices)
    deployment_mode: str = "local"

    def __post_init__(self) -> None:
        runtime_type = str(self.runtime_type or "").strip().lower()
        canonical_runtime_type = _RUNTIME_TYPE_ALIASES.get(
            runtime_type, runtime_type
        )
        object.__setattr__(self, "runtime_type", canonical_runtime_type)
        object.__setattr__(self, "project_dir", Path(self.project_dir))
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        deployment = str(
            os.environ.get("KSADK_DEPLOYMENT_MODE") or self.deployment_mode or "local"
        ).strip().lower()
        if deployment not in ("local", "ksadk_managed_cloud", "external_managed"):
            deployment = "local"
        object.__setattr__(self, "deployment_mode", deployment)


__all__ = ["RuntimeLaunchContext", "RuntimeServices"]
