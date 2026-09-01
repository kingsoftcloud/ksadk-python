"""Studio lifecycle API for DSH plugins and the Codex compatibility bridge."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import FastAPI, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ksadk.plugins.bridges.codex import (
    CodexAppServerPluginBridge,
    CodexBridgeHost,
    CodexPluginApprovalRequired,
    CodexPluginDetail,
    CodexPluginInventory,
    CodexPluginNotFoundError,
)
from ksadk.plugins.bridges.dsh import (
    DshBridgeHost,
    DshHostUnavailableError,
    DshPluginApprovalRequired,
    DshPluginInventory,
    DshPluginMutationError,
    DshPluginNotFoundError,
    DshProfilePluginBridge,
)
from ksadk.plugins.dsh_toolchain import DshToolchainError, DshToolchainManager
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


class CodexPluginInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    marketplace_name: str | None = Field(
        default=None, alias="marketplaceName", min_length=1, max_length=256
    )
    accept_undeclared_permissions: bool = Field(default=False, alias="acceptUndeclaredPermissions")


class DshPluginInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source: str = Field(min_length=1, max_length=2048)
    accept_host_permissions: bool = Field(default=False, alias="acceptHostPermissions")

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("source must be one package, Git URL, or absolute local path")
        return normalized


class DshPluginUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    accept_host_permissions: bool = Field(default=False, alias="acceptHostPermissions")


def _studio_codex_home(studio: StudioService) -> tuple[Path, str]:
    configured = os.environ.get("KSADK_CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser(), "explicit"
    return studio.workspace.root / ".agentkit" / "codex-home", "workspace-isolated"


def _studio_dsh_options(studio: StudioService) -> tuple[Path, str, tuple[str, ...] | None, str]:
    configured_home = os.environ.get("KSADK_DSH_HOME", "").strip()
    home = (
        Path(configured_home).expanduser()
        if configured_home
        else studio.workspace.root / ".agentkit" / "dsh-home"
    )
    configured_bin = os.environ.get("KSADK_DSH_BIN", "").strip()
    if configured_bin:
        command = (str(Path(configured_bin).expanduser()),)
    else:
        try:
            command = DshToolchainManager().require_command()
        except (DshToolchainError, OSError, ValueError):
            # Keep the bridge's established PATH lookup when the optional
            # pinned toolchain has not been installed or is unusable.
            command = None
    profile = os.environ.get("KSADK_DSH_PROFILE", "").strip() or "studio"
    return home, profile, command, "explicit" if configured_home else "workspace-isolated"


def _public_host(kind: str, host: Any | None, *, home_mode: str) -> dict[str, Any]:
    codex = kind == "codex"
    return {
        "hostId": "codex-app-server" if codex else "deepseek-harness",
        "available": host is not None,
        "status": "available" if host is not None else "unavailable",
        "version": host.version if host is not None else None,
        "protocol": host.protocol
        if host is not None
        else ("codex.app-server/v1" if codex else "dsh.profile/v1"),
        "homeMode": home_mode,
    }


def _public_codex_source(source: BaseModel) -> dict[str, Any]:
    raw = source.model_dump(by_alias=True, mode="json")
    source_type = str(raw.get("type") or "remote")
    payload: dict[str, Any] = {"type": source_type}
    if source_type == "local":
        payload["name"] = Path(str(raw.get("path") or "plugin")).name or "plugin"
    elif source_type == "git":
        payload["name"] = Path(urlparse(str(raw.get("url") or "")).path.rstrip("/")).name
        for key in ("refName", "sha"):
            if raw.get(key):
                payload[key] = raw[key]
    elif source_type == "npm":
        payload.update(package=raw.get("package"), version=raw.get("version"))
    return payload


def _public_codex_inventory(
    inventory: CodexPluginInventory, *, host: CodexBridgeHost, home_mode: str
) -> dict[str, Any]:
    return {
        "ecosystem": "codex",
        "integrationMode": "bridged",
        "pluginId": inventory.plugin_id,
        "resolvedVersion": inventory.version,
        "distributionName": inventory.name,
        "displayName": inventory.name,
        "marketplaceName": inventory.marketplace_name,
        "source": _public_codex_source(inventory.source),
        "installed": inventory.installed,
        "state": "enabled" if inventory.enabled else "disabled",
        "enabled": inventory.enabled,
        "availability": inventory.availability,
        "permissions": [],
        "permissionsDeclared": False,
        "riskDisclosures": list(inventory.risk_disclosures),
        "isolation": "host-managed",
        "runtimeState": None,
        "host": _public_host("codex", host, home_mode=home_mode),
    }


def _public_codex_detail(
    detail: CodexPluginDetail, *, host: CodexBridgeHost, home_mode: str
) -> dict[str, Any]:
    return {
        "item": _public_codex_inventory(detail.inventory, host=host, home_mode=home_mode),
        "description": detail.description,
        "capabilities": {
            "skills": list(detail.skills),
            "mcpServers": list(detail.mcp_servers),
            "hooks": list(detail.hooks),
            "apps": list(detail.apps),
            "scheduledTasks": list(detail.scheduled_tasks),
        },
    }


def _public_dsh_inventory(
    inventory: DshPluginInventory,
    *,
    host: DshBridgeHost,
    home_mode: str,
    runtime_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = inventory.client_bundle
    return {
        "ecosystem": "dsh",
        "integrationMode": "bridged",
        "pluginId": inventory.name,
        "resolvedVersion": inventory.version,
        "distributionName": inventory.name,
        "displayName": inventory.display_name,
        "description": inventory.description,
        "profile": inventory.profile,
        "source": {"type": "host-profile", "name": inventory.name},
        "installed": True,
        "state": "enabled" if inventory.enabled else "disabled",
        "enabled": inventory.enabled,
        "permissions": [],
        "permissionsDeclared": False,
        "riskDisclosures": list(inventory.risk_disclosures),
        "isolation": "host-managed",
        "runtimeState": runtime_state,
        "clientBundle": (
            {
                "platform": client.platform,
                "digest": client.digest,
                "contentBytes": client.content_bytes,
                "external": list(client.external),
                "inject": list(client.inject),
                "compatible": client.compatible,
                "incompatibilityReason": client.incompatibility_reason or None,
            }
            if client is not None
            else None
        ),
        "host": _public_host("dsh", host, home_mode=home_mode),
    }


def _public_dsh_client_bundle(inventory: DshPluginInventory) -> dict[str, Any] | None:
    client = inventory.client_bundle
    if client is None:
        return None
    query = urlencode({"pluginName": inventory.name, "digest": client.digest})
    return {
        "pluginId": inventory.name,
        "enabled": inventory.enabled,
        "compatible": client.compatible,
        "digest": client.digest,
        "contentBytes": client.content_bytes,
        "external": list(client.external),
        "inject": list(client.inject),
        "incompatibilityReason": client.incompatibility_reason or None,
        "url": f"/api/v1/plugin-ecosystems/dsh/client-bundle?{query}",
    }


def _codex_error(error: Exception) -> StudioError:
    if isinstance(error, CodexPluginNotFoundError):
        return StudioError(
            "CODEX_PLUGIN_NOT_FOUND", "Codex 插件不存在或来源不唯一", status_code=404
        )
    if isinstance(error, CodexPluginApprovalRequired):
        return StudioError(
            "CODEX_PLUGIN_RISK_CONFIRMATION_REQUIRED",
            "安装 Codex 插件前必须确认宿主权限风险",
            status_code=422,
        )
    return StudioError("CODEX_PLUGIN_HOST_UNAVAILABLE", "Codex 插件宿主当前不可用", status_code=503)


def _dsh_error(error: Exception) -> StudioError:
    if isinstance(error, DshPluginNotFoundError):
        return StudioError("DSH_PLUGIN_NOT_FOUND", "DSH 插件未安装", status_code=404)
    if isinstance(error, DshPluginApprovalRequired):
        return StudioError(
            "DSH_PLUGIN_RISK_CONFIRMATION_REQUIRED",
            "安装或升级 DSH 插件前必须确认宿主权限风险",
            status_code=422,
        )
    if isinstance(error, DshPluginMutationError):
        return StudioError(
            "DSH_PLUGIN_MUTATION_FAILED", "DSH 插件操作失败，原 Profile 已保留", status_code=409
        )
    if isinstance(error, (DshHostUnavailableError, OSError)):
        return StudioError("DSH_PLUGIN_HOST_UNAVAILABLE", "DSH 插件宿主当前不可用", status_code=503)
    return StudioError("DSH_PLUGIN_OPERATION_FAILED", "DSH 插件操作失败", status_code=422)


def register_plugin_routes(app: FastAPI, studio: StudioService) -> None:
    """Register only DSH Profile and Codex App Server lifecycle routes."""

    codex_home, codex_mode = _studio_codex_home(studio)
    dsh_home, dsh_profile, dsh_command, dsh_mode = _studio_dsh_options(studio)

    def public_dsh(item: DshPluginInventory, host: DshBridgeHost) -> dict[str, Any]:
        return _public_dsh_inventory(
            item,
            host=host,
            home_mode=dsh_mode,
            runtime_state=studio.dsh_provider_runtime_state(item.name),
        )

    def call_dsh(operation: Callable[[DshProfilePluginBridge], Any]) -> Any:
        try:
            with DshProfilePluginBridge(
                dsh_home=dsh_home, profile=dsh_profile, dsh_command=dsh_command
            ) as bridge:
                return bridge.host, operation(bridge)
        except Exception as error:
            raise _dsh_error(error) from None

    @app.get("/api/v1/plugin-ecosystems/codex/plugins")
    async def list_codex_plugins(
        installed_only: bool = Query(default=False), force_refetch: bool = Query(default=False)
    ):
        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                items = await bridge.list_plugins(force_refetch=force_refetch)
                visible = [item for item in items if item.installed or not installed_only]
                return {
                    "ecosystem": "codex",
                    "integrationMode": "bridged",
                    "host": _public_host("codex", bridge.host, home_mode=codex_mode),
                    "items": [
                        _public_codex_inventory(item, host=bridge.host, home_mode=codex_mode)
                        for item in visible
                    ],
                }
        except Exception:
            return {
                "ecosystem": "codex",
                "integrationMode": "bridged",
                "host": _public_host("codex", None, home_mode=codex_mode),
                "items": [],
                "error": {
                    "code": "CODEX_PLUGIN_HOST_UNAVAILABLE",
                    "message": "Codex 插件宿主当前不可用",
                },
            }

    @app.get("/api/v1/plugin-ecosystems/codex/plugins/{plugin_id}")
    async def get_codex_plugin(
        plugin_id: str, marketplace_name: str | None = Query(default=None, max_length=256)
    ):
        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                detail = await bridge.read_plugin(plugin_id, marketplace_name=marketplace_name)
                return _public_codex_detail(detail, host=bridge.host, home_mode=codex_mode)
        except Exception as error:
            raise _codex_error(error) from None

    @app.post("/api/v1/plugin-ecosystems/codex/plugins/{plugin_id}:install")
    async def install_codex_plugin(plugin_id: str, payload: CodexPluginInstallRequest):
        if not payload.accept_undeclared_permissions:
            raise _codex_error(CodexPluginApprovalRequired("approval required"))
        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                result = await bridge.install_plugin(
                    plugin_id,
                    marketplace_name=payload.marketplace_name,
                    accept_undeclared_permissions=True,
                    install_attempt_id=f"studio-{uuid4().hex}",
                )
                return {
                    "item": _public_codex_inventory(
                        result.inventory, host=bridge.host, home_mode=codex_mode
                    ),
                    "authPolicy": result.auth_policy,
                    "appsNeedingAuth": list(result.apps_needing_auth),
                }
        except Exception as error:
            raise _codex_error(error) from None

    @app.delete("/api/v1/plugin-ecosystems/codex/plugins/{plugin_id}", status_code=204)
    async def uninstall_codex_plugin(plugin_id: str):
        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                await bridge.uninstall_plugin(plugin_id)
            return Response(status_code=204)
        except Exception as error:
            raise _codex_error(error) from None

    @app.get("/api/v1/plugin-ecosystems/dsh/plugins")
    async def list_dsh_plugins():
        try:
            host, items = await asyncio.to_thread(call_dsh, lambda bridge: bridge.list_plugins())
            return {
                "ecosystem": "dsh",
                "integrationMode": "bridged",
                "profile": dsh_profile,
                "host": _public_host("dsh", host, home_mode=dsh_mode),
                "items": [public_dsh(item, host) for item in items],
            }
        except StudioError as error:
            return {
                "ecosystem": "dsh",
                "integrationMode": "bridged",
                "profile": dsh_profile,
                "host": _public_host("dsh", None, home_mode=dsh_mode),
                "items": [],
                "error": {"code": error.code, "message": error.message},
            }

    @app.get("/api/v1/plugin-ecosystems/dsh/profile")
    async def get_dsh_profile_projection():
        host, result = await asyncio.to_thread(
            call_dsh, lambda bridge: (bridge.project_profile(), bridge.list_plugins())
        )
        projection, items = result
        bundles = [
            projected
            for item in items
            if item.enabled
            for projected in [_public_dsh_client_bundle(item)]
            if projected is not None
        ]
        graph_hash = hashlib.sha256(projection.config_digest.encode("utf-8"))
        for bundle in bundles:
            for value in (str(bundle["pluginId"]), str(bundle["digest"])):
                encoded = value.encode("utf-8")
                graph_hash.update(f"{len(encoded)}:".encode("ascii"))
                graph_hash.update(encoded)
        return {
            "host": _public_host("dsh", host, home_mode=dsh_mode),
            "profile": projection.model_dump(mode="json", by_alias=True),
            "clientGraphDigest": f"sha256:{graph_hash.hexdigest()}",
            "clientBundles": bundles,
        }

    @app.get("/api/v1/plugin-ecosystems/dsh/client-bundle")
    async def get_dsh_client_bundle(
        plugin_name: str = Query(alias="pluginName", min_length=1, max_length=256),
        digest: str = Query(pattern=r"^sha256:[0-9a-f]{64}$"),
    ):
        _, content = await asyncio.to_thread(
            call_dsh,
            lambda bridge: bridge.read_client_bundle(plugin_name, expected_digest=digest),
        )
        return Response(
            content=content,
            media_type="application/javascript; charset=utf-8",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "ETag": f'"{digest}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins:install", status_code=201)
    async def install_dsh_plugin(payload: DshPluginInstallRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))
        host, item = await asyncio.to_thread(
            call_dsh,
            lambda bridge: bridge.install_plugin(payload.source, accept_host_permissions=True),
        )
        await studio.refresh_dsh_provider_registrations()
        return {"item": public_dsh(item, host)}

    async def mutate_dsh(plugin_name: str, operation: str):
        def action(bridge: DshProfilePluginBridge):
            if operation == "enable":
                return bridge.set_enabled(plugin_name, enabled=True)
            if operation == "disable":
                return bridge.set_enabled(plugin_name, enabled=False)
            if operation == "uninstall":
                return bridge.uninstall_plugin(plugin_name)
            return bridge.get_plugin(plugin_name)

        return await asyncio.to_thread(call_dsh, action)

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:enable")
    async def enable_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "enable")
        await studio.refresh_dsh_provider_registrations()
        return {"item": public_dsh(item, host)}

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:disable")
    async def disable_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "disable")
        await studio.refresh_dsh_provider_registrations()
        return {"item": public_dsh(item, host)}

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:update")
    async def update_dsh_plugin(plugin_name: str, payload: DshPluginUpdateRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))
        host, item = await asyncio.to_thread(
            call_dsh,
            lambda bridge: bridge.update_plugin(plugin_name, accept_host_permissions=True),
        )
        await studio.refresh_dsh_provider_registrations()
        return {"item": public_dsh(item, host)}

    @app.get("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}")
    async def get_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "get")
        return {"item": public_dsh(item, host)}

    @app.delete("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}", status_code=204)
    async def uninstall_dsh_plugin(plugin_name: str):
        await mutate_dsh(plugin_name, "uninstall")
        await studio.refresh_dsh_provider_registrations()
        return Response(status_code=204)


__all__ = [
    "CodexPluginInstallRequest",
    "DshPluginInstallRequest",
    "DshPluginUpdateRequest",
    "register_plugin_routes",
]
