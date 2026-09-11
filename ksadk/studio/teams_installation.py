"""Composition wiring for the official Teams workspace plugin.

Domain, tools, routes, and policy remain in the plugin package. This adapter
supplies Studio's authenticated local principal, resource catalog, lifecycle
maintenance, and UI/API slots.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from ksadk.plugins.dsh_teams import (
    TEAMS_PLUGIN_ID,
    configure_teams_profile,
    teams_companion_definition,
)
from ksadk.plugins.teams.api import create_router
from ksadk.plugins.teams.application import TeamsApplication
from ksadk.plugins.teams.contracts import API_VERSION, Actor
from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.workspace_plugins import WorkspacePlugin


class StudioTeamsInstallation:
    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.available = callable(getattr(studio.dsh_capabilities, "configure_companions", None))
        self.authority_ref = (
            "studio:" + hashlib.sha256(str(studio.workspace.root).encode()).hexdigest()[:24]
        )
        self.application = TeamsApplication(
            path=studio.workspace.resolve(".agentkit/plugins/teams/teams.sqlite"),
            authority_ref=self.authority_ref,
            host=studio.execution_host,
            actor=lambda: Actor("local-studio", "local-user"),
            list_build_ids=lambda: [build.id for build in studio.builds.list()],
            workspace_root=studio.workspace.resolve(".agentkit/plugins/teams/workspaces"),
            tool_transport=self._tool_transport,
            require_artifact=True,
        )
        definition = teams_companion_definition(self.application)
        from dataclasses import replace

        self.definition = replace(
            definition, start=self._activate, revoke=self._revoke, close=self._close
        )
        self._contributed = False
        self._failure: str | None = None
        studio.workspace_plugins.register(
            WorkspacePlugin(
                "teams", API_VERSION, self.enable, self.disable, self.status, shutdown=self._close
            )
        )
        if self.available:
            try:
                studio.dsh_capabilities.configure_companions([self.definition])
            except Exception:
                self.available = False
                self._failure = "authority_profile_unavailable"

    def status(self) -> dict[str, Any]:
        running = self.application.runtime.domain is not None and self._contributed
        manager = getattr(self.studio.dsh_capabilities, "companion_manager", None)
        active = bool(manager and TEAMS_PLUGIN_ID in manager.active_plugins)
        return {
            "enabled": running and active,
            "available": self.available,
            "health": "ready" if running and active else "disabled",
            "authorityRef": self.authority_ref,
            "reason": self._failure or self.application.runtime.last_error,
        }

    async def _activate(self) -> None:
        await self.application.start()
        self.studio.workspace_plugins.contribute_routes("teams", create_router(self.application))
        self._contributed = True
        self._failure = None

    async def _revoke(self) -> None:
        self.studio.workspace_plugins.remove_routes("teams")
        self._contributed = False
        await self.application.revoke()

    async def _close(self) -> None:
        await self._revoke()
        await self.application.close()

    async def _tool_transport(self, principal, operation, arguments, call_id):
        return await self.studio.dsh_capabilities.call_companion_tool(
            TEAMS_PLUGIN_ID, principal, operation, arguments, call_id=call_id
        )

    async def _configure(self, enabled: bool) -> None:
        if not self.available:
            raise TeamsError(
                "authority_profile_unavailable", "Teams 需要本地 web 权威 Profile", status=503
            )
        await self.studio.start()
        from ksadk.studio.api_plugin_routes import _studio_dsh_options

        home, profile, command, _ = _studio_dsh_options(self.studio)
        if profile != "web" or not command:
            raise TeamsError(
                "dsh_toolchain_required", "请先安装并启用受支持的 DSH 工具链", status=503
            )

        async def operation():
            return await asyncio.to_thread(
                configure_teams_profile,
                workspace=self.studio.workspace.root,
                dsh_home=home,
                dsh_command=command,
                enabled=enabled,
            )

        await self.studio.reconfigure_dsh_profile(operation)
        await self.studio.dsh_capabilities.capability_snapshot()
        if enabled and not self.status()["enabled"]:
            raise TeamsError("teams_activation_incomplete", "Teams 插件图尚未完整激活", status=503)

    async def enable(self) -> None:
        if self.status()["enabled"]:
            return
        await self._configure(True)

    async def disable(self) -> None:
        if not self.status()["enabled"] and self.application.runtime.domain is None:
            return
        await self._configure(False)
