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
from ksadk.studio.plugin_lifecycle import RECOVERY_URL, lifecycle_error, lifecycle_failure
from ksadk.studio.teams_catalog import StudioTeamsCatalog
from ksadk.studio.workspace_plugins import WorkspacePlugin


def create_teams_installation(studio: Any):
    """Choose the explicitly configured authority before registering routes."""
    server_url = studio.configuration.environment().get("KSADK_TEAMS_SERVER_URL", "").strip()
    if server_url:
        from ksadk.studio.teams_node_factory import create_studio_teams_node
        from ksadk.studio.teams_remote import RemoteStudioTeamsInstallation

        return RemoteStudioTeamsInstallation(
            studio, server_url, node_v1_factory=create_studio_teams_node
        )
    return StudioTeamsInstallation(studio)


class StudioTeamsInstallation:
    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.available = callable(getattr(studio.dsh_capabilities, "configure_companions", None))
        self.authority_ref = (
            "studio:" + hashlib.sha256(str(studio.workspace.root).encode()).hexdigest()[:24]
        )
        self.catalog = StudioTeamsCatalog(studio, authority_ref=self.authority_ref)
        self.application = TeamsApplication(
            path=studio.workspace.resolve(".agentkit/plugins/teams/teams.sqlite"),
            authority_ref=self.authority_ref,
            host=studio.execution_host,
            actor=lambda: Actor("local-studio", "local-user"),
            list_build_ids=lambda: [build.id for build in studio.builds.list()],
            workspace_root=studio.workspace.resolve(".agentkit/plugins/teams/workspaces"),
            tool_transport=self._tool_transport,
            require_artifact=True,
            list_bindings=self.catalog.list_bindings,
            allowed_workspace_roots=[studio.workspace.root],
        )
        definition = teams_companion_definition(self.application)
        from dataclasses import replace

        self.definition = replace(
            definition, start=self._activate, revoke=self._revoke, close=self._close
        )
        self._contributed = False
        self._failure: dict[str, Any] | None = None
        self._configured_enabled: bool | None = None
        self._stage = "registered"
        studio.workspace_plugins.register(
            WorkspacePlugin(
                "teams",
                API_VERSION,
                self.enable,
                self.disable,
                self.status,
                shutdown=self._close,
                repair=self.repair,
            )
        )
        if self.available:
            try:
                studio.dsh_capabilities.configure_companions([self.definition])
            except Exception as error:
                self.available = False
                self._failure = lifecycle_failure(error, "configuration")

    def status(self) -> dict[str, Any]:
        running = self.application.runtime.domain is not None and self._contributed
        manager = getattr(self.studio.dsh_capabilities, "companion_manager", None)
        active = bool(manager and TEAMS_PLUGIN_ID in manager.active_plugins)
        host_status = getattr(self.studio.dsh_capabilities, "startup_status", {})
        failure = self._failure or (
            host_status.get("failure") if self._configured_enabled is not False else None
        )
        return {
            "enabled": running and active,
            "available": self.available,
            "health": "ready" if running and active else "failed" if failure else "disabled",
            "configuredEnabled": True if running and active else self._configured_enabled,
            "stage": failure["stage"] if failure else self._stage,
            "authorityRef": self.authority_ref,
            "reason": failure["code"] if failure else self.application.runtime.last_error,
            "failure": failure,
            "recoveryUrl": RECOVERY_URL,
        }

    async def _activate(self) -> None:
        self._configured_enabled = True
        self._stage = "companion_start"
        try:
            await self.application.start()
            self.studio.workspace_plugins.contribute_routes(
                "teams", create_router(self.application)
            )
        except Exception as error:
            self._failure = lifecycle_failure(error, self._stage)
            raise lifecycle_error(error, self._stage) from error
        self._contributed = True
        self._failure = None
        self._stage = "ready"

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
        self._stage = "profile_preflight"
        try:
            await self._configure_profile(enabled)
        except Exception as error:
            self._failure = lifecycle_failure(error, self._stage)
            raise lifecycle_error(error, self._stage) from error

    async def _configure_profile(self, enabled: bool) -> None:
        if enabled and not self.available:
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
            result = await asyncio.to_thread(
                configure_teams_profile,
                workspace=self.studio.workspace.root,
                dsh_home=home,
                dsh_command=command,
                enabled=enabled,
            )
            self._configured_enabled = enabled
            return result

        self._stage = "profile_configuration"
        try:
            await self.studio.reconfigure_dsh_profile(operation)
            self._configured_enabled = enabled
            # Disabling a failed companion must not require another successful Core boot.
            if enabled:
                self._stage = "companion_start"
                await self.studio.dsh_capabilities.capability_snapshot()
                if not self.status()["enabled"]:
                    raise TeamsError(
                        "teams_activation_incomplete", "Teams 插件图尚未完整激活", status=503
                    )
        except Exception as error:
            self._failure = lifecycle_failure(error, self._stage)
            raise lifecycle_error(error, self._stage) from error
        self._failure = None
        self._stage = "ready" if enabled else "disabled"

    async def enable(self) -> None:
        if self.status()["enabled"]:
            return
        await self._configure(True)

    async def repair(self):
        from ksadk.plugins.teams.migration_cli import upgrade_local_history

        if self.status()["enabled"]:
            return {"status": "already_current"}
        if (self.status().get("failure") or {}).get("code") != "artifact_migration_required":
            raise TeamsError("repair_unavailable", "当前故障不适用历史版本兼容升级", status=409)
        self._stage = "data_migration"
        result = await asyncio.to_thread(
            upgrade_local_history,
            self.application.runtime.path,
            authority_ref=self.authority_ref,
            plugin_digest=self.application.runtime.plugin_digest,
        )
        await self.studio.dsh_capabilities.capability_snapshot()
        if not self.status()["enabled"]:
            raise TeamsError(
                "teams_activation_incomplete", "历史备份升级完成，请重试插件启动", status=503
            )
        return result

    async def disable(self) -> None:
        # Runtime absence does not imply disabled Profile flags (e.g. failed activation).
        await self._configure(False)
