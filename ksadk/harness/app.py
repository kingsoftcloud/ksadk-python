"""RuntimeAdapter-first deployable Harness composition root."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from fastapi import FastAPI

from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import HarnessReasoner
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.runtime import (
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.factory import create_runtime_adapter
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.server.routes.routers import GROUP_ROUTERS, load_route_modules


class HarnessPlugin(Protocol):
    def before_build(self, config: HarnessConfig) -> None:  # pragma: no cover
        ...

    def after_build(self, app: FastAPI, config: HarnessConfig) -> None:  # pragma: no cover
        ...


_OVERRIDEABLE = ("model", "prompt")


@dataclass(frozen=True)
class HarnessCapabilities:
    responses: bool = True
    sessions: bool = True
    files: bool = False
    a2a: bool = False
    agui: bool = False


class HarnessApp:
    """Own one adapter/executor/context tuple for a deployable YAML agent."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        plugins: Sequence[HarnessPlugin] = (),
        adapter_builder: Optional[
            Callable[[HarnessConfig, Path, HarnessReasoner | None], RuntimeAdapter]
        ] = None,
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
        self._adapter: RuntimeAdapter | None = None
        self._fastapi_app: FastAPI | None = None
        self._capabilities = HarnessCapabilities(a2a=a2a is not None)
        self._runtime_type = "codex" if config.runtime == "codex" else "harness"
        self._launch_context = RuntimeLaunchContext(
            runtime_type=self._runtime_type,
            project_dir=self._workspace_root,
            config={
                "model": config.model,
                "base_instructions": config.prompt,
                "sandbox_read_only": config.sandbox.read_only,
            },
        )
        registry = RuntimeRegistry()
        registry.register(self._runtime_type, lambda _context: self.adapter())
        self._executor = RuntimeExecutor(registry)

        from ksadk.sessions import create_session_service

        self._session_service = create_session_service(
            backend="memory",
            project_dir=str(self._workspace_root),
        )

    @classmethod
    def from_yaml(cls, path: str | Path, **kwargs: Any) -> "HarnessApp":
        return cls(HarnessConfig.from_yaml(path), **kwargs)

    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def session_service(self) -> Any:
        return self._session_service

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    @property
    def executor(self) -> RuntimeExecutor:
        return self._executor

    @property
    def launch_context(self) -> RuntimeLaunchContext:
        return self._launch_context

    def apply_overrides(self, overrides: Optional[dict[str, Any]]) -> HarnessConfig:
        if not overrides:
            return self._config
        unknown = set(overrides) - set(_OVERRIDEABLE)
        if unknown:
            raise ValueError(
                f"override 仅允许 {list(_OVERRIDEABLE)}(最小子集),不支持: {sorted(unknown)}"
            )
        return replace(self._config, **overrides)

    def adapter(self, overrides: Optional[dict[str, Any]] = None) -> RuntimeAdapter:
        if overrides is not None:
            raise ValueError("adapter 不接受 invocation override;请传给 start(request)")
        if self._adapter is not None:
            return self._adapter
        for plugin in self._plugins:
            plugin.before_build(self._config)
        if self._adapter_builder is not None:
            adapter = self._adapter_builder(
                self._config,
                self._workspace_root,
                self._reasoner,
            )
        elif self._runtime_type == "codex":
            adapter = create_runtime_adapter(self._launch_context)
        else:
            adapter = HarnessRuntimeAdapter(
                self._config,
                reasoner=self._reasoner,
                workspace_root=self._workspace_root,
            )
        if not isinstance(adapter, RuntimeAdapter):
            raise TypeError("Harness adapter builder must return RuntimeAdapter")
        self._adapter = adapter
        return adapter

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
        self,
        request: StartRequest,
        overrides: Optional[dict[str, Any]],
    ) -> StartRequest:
        effective = self.apply_overrides(overrides)
        metadata = dict(request.metadata or {})
        model = request.model or effective.model
        prompt = str(
            request.config.get("base_instructions")
            or request.config.get("instructions")
            or request.config.get("prompt")
            or effective.prompt
        )
        metadata["model_override"] = model
        metadata["prompt_override"] = prompt
        config = dict(request.config)
        config["base_instructions"] = prompt
        return request.model_copy(
            update={
                "model": model,
                "config": config,
                "metadata": metadata,
            }
        )

    async def start(
        self,
        request: StartRequest,
        overrides: Optional[dict[str, Any]] = None,
    ):
        return await self._executor.start(
            self._launch_context,
            self._request_with_overrides(request, overrides),
        )

    def stream(self, handle: Any):
        return self._executor.stream(handle)

    async def cancel(self, handle: Any):
        return await self._executor.cancel(handle)

    async def resume(self, handle: Any, target: Any, payload: Any = None):
        return await self._executor.resume(handle, target, payload)

    async def checkpoint(self, handle: Any):
        return await self._executor.checkpoint(handle)

    async def close(self, handle: Any):
        return await self._executor.close(handle)

    @staticmethod
    def _configure_routes(app: FastAPI, state: Any, groups: set[str]) -> None:
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
        if overrides is not None:
            raise ValueError("build_app 不接受 invocation override;请传给 start(request)")
        if self._fastapi_app is not None:
            return self._fastapi_app
        adapter = self.adapter()
        app = create_runtime_app(
            RuntimeAppConfig(
                runtime_type=self._runtime_type,
                route_groups=self._route_groups(),
                a2a=self._a2a,
                a2a_runtime_adapter=adapter if self._a2a is not None else None,
                runtime_executor=self._executor,
                launch_context=self._launch_context,
                session_backend_provider=lambda: {
                    "Backend": "memory",
                    "Shared": False,
                    "ProductionSafe": False,
                    "ContinuityDefault": "local_only",
                },
            ),
            self._configure_routes,
        )
        app.state.runtime.session_service = self._session_service
        app.state.runtime.harness_capabilities = self._capabilities
        for plugin in self._plugins:
            plugin.after_build(app, self._config)
        self._fastapi_app = app
        return app


__all__ = ["HarnessApp", "HarnessCapabilities", "HarnessPlugin"]
