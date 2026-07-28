"""HarnessApp — 统一 Runtime 的可部署交付物 / composition root。

H2 §4.2:HarnessApp 是 composition root,**不包装复制 3000+ 行全局 app**——复用
G0.1 ``create_runtime_app`` 装配;A2A、Responses、session、files 等**数据面** route
group 装配,**控制面 action 不进入**;不 import 全局 ``base_app`` 再过滤复制路由。

本层保持 AgentDraft 的最小字段面，但执行路径会运行真实模型推理、MCP transport 与
Harness read-only sandbox policy。不含完整 AgentDraft v1 字段面、plugin 生态或 deploy 自动化。

分层职责:yaml → RuntimeAdapter ``start(request)`` 的映射在**本层**;平台请求 →
codex 配置(config.toml/AGENTS.md/mcp_servers)的翻译是 goal-09 CodexRuntime 的职责,
不在本层做。
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from fastapi import FastAPI

from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import HarnessReasoner
from ksadk.harness.runner import YamlAgentRunner
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import RuntimeAdapter, StartRequest
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.server.routes.routers import GROUP_ROUTERS, load_route_modules

logger = logging.getLogger(__name__)


class HarnessPlugin(Protocol):
    """基础 plugin host 的插件协议(骨架期最小钩子)。"""

    def before_build(self, config: HarnessConfig) -> None:  # pragma: no cover - 协议
        ...

    def after_build(self, app: FastAPI, config: HarnessConfig) -> None:  # pragma: no cover
        ...


#: per-invocation override 允许覆盖的字段(最小子集内)。
_OVERRIDEABLE = ("model", "prompt")


@dataclass(frozen=True)
class HarnessCapabilities:
    """Protocol/data surfaces backed by this Harness composition."""

    responses: bool = True
    sessions: bool = True
    files: bool = False
    a2a: bool = False
    agui: bool = False


class HarnessApp:
    """统一 Runtime 的 composition root(skeleton)。"""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        plugins: Sequence[HarnessPlugin] = (),
        adapter_builder: Optional[Callable[[BaseRunner], RuntimeAdapter]] = None,
        reasoner: HarnessReasoner | None = None,
        workspace_root: str | Path | None = None,
        a2a: Any | None = None,
    ) -> None:
        self._config = config
        self._plugins = list(plugins)
        self._adapter_builder = adapter_builder
        self._reasoner = reasoner
        self._a2a = a2a
        self._owned_workspace: tempfile.TemporaryDirectory[str] | None
        if workspace_root is None:
            self._owned_workspace = tempfile.TemporaryDirectory(prefix="ksadk-harness-")
            resolved_workspace: str | Path = self._owned_workspace.name
        else:
            self._owned_workspace = None
            resolved_workspace = workspace_root
        self._workspace_root = Path(resolved_workspace).resolve()
        self._runner: BaseRunner | None = None
        self._adapter: RuntimeAdapter | None = None
        self._fastapi_app: FastAPI | None = None
        self._capabilities = HarnessCapabilities(a2a=a2a is not None)
        # Each Harness owns its session backend.  Route handlers may use this
        # marker even when a host does not provide the optional session seam.
        from ksadk.sessions import create_session_service

        self._session_service = create_session_service(
            backend="memory", project_dir=str(self._workspace_root)
        )

    @classmethod
    def from_yaml(cls, path: str | Path, **kwargs: Any) -> "HarnessApp":
        """从 yaml 装配(最小子集,超出明确报错)。"""
        return cls(HarnessConfig.from_yaml(path), **kwargs)

    @property
    def config(self) -> HarnessConfig:
        return self._config

    # ---- override ----

    def apply_overrides(self, overrides: Optional[dict[str, Any]]) -> HarnessConfig:
        """per-invocation override:只允许最小子集内字段,超出报错(不静默忽略)。"""
        if not overrides:
            return self._config
        unknown = set(overrides) - set(_OVERRIDEABLE)
        if unknown:
            raise ValueError(
                f"override 仅允许 {list(_OVERRIDEABLE)}(最小子集),不支持: {sorted(unknown)}"
            )
        return replace(self._config, **overrides)

    # ---- 组装 ----

    def build_runner(self, overrides: Optional[dict[str, Any]] = None) -> BaseRunner:
        if overrides is not None:
            raise ValueError("build_runner 不接受 invocation override;请传给 start(request)")
        if self._runner is None:
            self._runner = self._build_runner(self._config)
        return self._runner

    def _build_runner(self, config: HarnessConfig) -> BaseRunner:
        for plugin in self._plugins:
            plugin.before_build(config)
        if config.runtime == "codex":
            # codex runtime:CodexRunner(经 ksadk web 已验证路径),detector stub 用 CODEX
            from ksadk.detection.detector import FrameworkType
            from ksadk.runners.codex_runner import CodexRunner

            detection = type(
                "D", (), {"name": config.model, "type": FrameworkType.CODEX}
            )()
            return CodexRunner(detection, str(self._workspace_root))
        return YamlAgentRunner(
            config,
            reasoner=self._reasoner,
            workspace_root=self._workspace_root,
        )

    def adapter(self, overrides: Optional[dict[str, Any]] = None) -> RuntimeAdapter:
        """yaml → RuntimeAdapter(goal-07);start(request) 在此层映射。"""
        if overrides is not None:
            raise ValueError("adapter 不接受 invocation override;请传给 start(request)")
        if self._adapter is not None:
            return self._adapter
        rt = "codex" if self._config.runtime == "codex" else "harness"

        def _default_builder(runner: BaseRunner) -> RuntimeAdapter:
            return RunnerRuntimeAdapter(runner, runtime_type=rt)

        builder = self._adapter_builder or _default_builder
        self._adapter = builder(self.build_runner())
        return self._adapter

    @property
    def runner(self) -> BaseRunner:
        """The one runner owned by this Harness composition root."""
        return self.build_runner()

    @property
    def session_service(self) -> Any:
        return self._session_service

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    def _route_groups(self) -> set[str]:
        groups = {"health_meta", "models", "feedback", "tools", "ui_bootstrap"}
        if self._capabilities.responses:
            groups.update({"run", "openai_compat"})
        if self._capabilities.sessions:
            groups.update({"sessions", "sessions_adk_compat"})
        if self._capabilities.files:
            groups.add("workspace")
        if self._capabilities.agui:
            groups.add("agui")
        return groups

    def _request_with_overrides(
        self, request: StartRequest, overrides: Optional[dict[str, Any]]
    ) -> StartRequest:
        metadata = dict(request.metadata or {})
        changed = False
        if request.model:
            metadata["model_override"] = request.model
            changed = True
        request_prompt = request.config.get("prompt") or request.config.get("instructions")
        if request_prompt is not None:
            metadata["prompt_override"] = str(request_prompt)
            changed = True
        if overrides:
            config = self.apply_overrides(overrides)
            metadata["model_override"] = config.model
            metadata["prompt_override"] = config.prompt
            changed = True
        if not changed:
            return request
        if hasattr(request, "model_copy"):
            return request.model_copy(update={"metadata": metadata})
        return request.copy(update={"metadata": metadata})

    async def start(self, request: StartRequest, overrides: Optional[dict[str, Any]] = None):
        """yaml → RuntimeAdapter.start(request)(本层映射)。"""
        return await self.adapter().start(self._request_with_overrides(request, overrides))

    def stream(self, handle: Any):
        return self.adapter().stream(handle)

    async def cancel(self, handle: Any):
        return await self.adapter().cancel(handle)

    async def resume(self, handle: Any, target: Any, payload: Any = None):
        return await self.adapter().resume(handle, target, payload)

    async def checkpoint(self, handle: Any):
        return await self.adapter().checkpoint(handle)

    async def close(self, handle: Any):
        return await self.adapter().close(handle)

    def _configure_routes(self, app: FastAPI, state: Any, groups: set[str]) -> None:
        """Attach only route groups; never install integrated workspace/terminal routes."""
        del state
        load_route_modules()
        ordered = sorted(group for group in groups if group != "health_meta")
        if "health_meta" in groups:
            ordered.append("health_meta")
        for group in ordered:
            router = GROUP_ROUTERS.get(group)
            if router is not None:
                app.include_router(router)

    def build_app(self, overrides: Optional[dict[str, Any]] = None) -> FastAPI:
        """装配可部署 app:复用 create_runtime_app,只挂数据面 route group(控制面不进)。"""
        if overrides is not None:
            # Overrides belong to StartRequest and must never create a second
            # process-wide runner for the deployed app.
            raise ValueError("build_app 不接受 invocation override;请传给 start(request)")
        if self._fastapi_app is not None:
            return self._fastapi_app
        runner = self.build_runner()
        app = create_runtime_app(
            RuntimeAppConfig(
                runner=runner,
                runtime_type="codex" if self._config.runtime == "codex" else "harness",
                route_groups=self._route_groups(),
                a2a=self._a2a,
                runtime_adapter=self.adapter() if self._a2a is not None else None,
                session_backend_provider=lambda: {
                    "Backend": "memory",
                    "Shared": False,
                    "ProductionSafe": False,
                    "ContinuityDefault": "local_only",
                },
            ),
            self._configure_routes,
        )
        app.state.runtime.runtime_adapter = self.adapter()
        app.state.runtime.session_service = self._session_service
        app.state.runtime.harness_capabilities = self._capabilities

        for plugin in self._plugins:
            plugin.after_build(app, self._config)
        self._fastapi_app = app
        return app


__all__ = ["HarnessApp", "HarnessCapabilities", "HarnessPlugin"]
