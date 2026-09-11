"""Lifecycle and route slots for trusted, installed workspace plugins.

The router mounts after built-in Studio routes and inherits local session/CSRF
middleware. Contributions are disposed when their owning plugin goes away.
It does not interpret feature payloads or execute code from message data.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class WorkspacePlugin:
    plugin_id: str
    api_version: str
    enable: Callable[[], Awaitable[None]]
    disable: Callable[[], Awaitable[None]]
    status: Callable[[], dict[str, Any]]
    shutdown: Callable[[], Awaitable[None]] | None = None


class LifecycleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class WorkspacePluginRegistry:
    def __init__(self) -> None:
        self.api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._plugins: dict[str, WorkspacePlugin] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._routes: dict[str, list[Any]] = {}

        @self.api.get("/plugins/{plugin_id}/lifecycle")
        async def status(plugin_id: str):
            plugin = self._plugins.get(plugin_id)
            if not plugin:
                return JSONResponse(
                    {"error": {"code": "plugin_not_installed", "message": "工作区插件未安装"}},
                    status_code=404,
                )
            return {"apiVersion": plugin.api_version, **plugin.status()}

        @self.api.post("/plugins/{plugin_id}/lifecycle")
        async def lifecycle(plugin_id: str, payload: LifecycleInput):
            plugin = self._plugins.get(plugin_id)
            if not plugin:
                return JSONResponse(
                    {"error": {"code": "plugin_not_installed", "message": "工作区插件未安装"}},
                    status_code=404,
                )
            async with self._locks[plugin_id]:
                try:
                    await (plugin.enable() if payload.enabled else plugin.disable())
                except Exception as error:
                    code = getattr(error, "code", "plugin_lifecycle_failed")
                    messages = {
                        "DSH_PROFILE_IN_USE": (
                            "仍有执行或审批正在进行，请结束执行后再变更插件 Profile"
                        ),
                        "artifact_migration_required": (
                            "插件制品与现有团队数据不匹配。请恢复原插件锁定版本，"
                            "或备份后执行明确的版本迁移；现有数据未修改。"
                        ),
                    }
                    return JSONResponse(
                        {
                            "error": {
                                "code": code,
                                "message": messages.get(
                                    code, "插件未能完成装配，请查看插件状态后重试"
                                ),
                            }
                        },
                        status_code=getattr(error, "status_code", getattr(error, "status", 503)),
                    )
                return {"apiVersion": plugin.api_version, **plugin.status()}

    def register(self, plugin: WorkspacePlugin) -> Callable[[], None]:
        if plugin.plugin_id in self._plugins:
            raise ValueError("workspace plugin already registered")
        self._plugins[plugin.plugin_id] = plugin
        self._locks[plugin.plugin_id] = asyncio.Lock()

        def dispose() -> None:
            self.remove_routes(plugin.plugin_id)
            self._plugins.pop(plugin.plugin_id, None)
            self._locks.pop(plugin.plugin_id, None)

        return dispose

    def contribute_routes(self, plugin_id: str, router: APIRouter) -> Callable[[], None]:
        if plugin_id not in self._plugins:
            raise ValueError("only a registered plugin may contribute routes")
        self.remove_routes(plugin_id)
        before = {id(route) for route in self.api.router.routes}
        self.api.include_router(router)
        self._routes[plugin_id] = [
            route for route in self.api.router.routes if id(route) not in before
        ]
        return lambda: self.remove_routes(plugin_id)

    def remove_routes(self, plugin_id: str) -> None:
        removed = {id(route) for route in self._routes.pop(plugin_id, [])}
        self.api.router.routes[:] = [
            route for route in self.api.router.routes if id(route) not in removed
        ]

    async def close(self) -> None:
        for plugin in tuple(self._plugins.values()):
            result = (plugin.shutdown or plugin.disable)()
            if inspect.isawaitable(result):
                await result
        self._plugins.clear()
        self._locks.clear()
