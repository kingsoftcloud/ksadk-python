"""Studio lifecycle API for DSH plugins and the Codex compatibility bridge."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlencode, urlparse
from uuid import uuid4

from fastapi import FastAPI, Query, Request, Response
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
    validate_dsh_registry_source,
)
from ksadk.plugins.codex_manifest import (
    CodexInstalledPluginSnapshot,
    CodexPluginSourceCoordinate,
    snapshot_installed_codex_plugin,
)
from ksadk.plugins.dsh_toolchain import DshToolchainError, DshToolchainManager
from ksadk.studio.codex_plugin_store import (
    CodexWorkspacePluginSnapshot,
    component_selector,
    find_installed_codex_plugin_root,
)
from ksadk.studio.dsh_capability_service import dsh_ui_mcp_call_id
from ksadk.studio.dsh_ui_sandbox import (
    DSH_UI_PROTOCOL_VERSION,
    DshClientBundleExecution,
    DshUiErrorResponse,
    DshUiResponseError,
    DshUiSuccessResponse,
    dsh_ui_client_bundle_headers,
    render_dsh_ui_sandbox_document,
    select_dsh_client_bundle_execution,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService

DSH_UI_SANDBOX_FRAME_PATH = "/api/v1/plugin-ecosystems/dsh/sandbox/frame"
DSH_UI_SANDBOX_BUNDLE_PATH = "/api/v1/plugin-ecosystems/dsh/sandbox/client-bundle"


class CodexPluginInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    marketplace_name: str | None = Field(
        default=None, alias="marketplaceName", min_length=1, max_length=256
    )
    accept_undeclared_permissions: bool = Field(default=False, alias="acceptUndeclaredPermissions")


class CodexPluginSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    marketplace_name: str | None = Field(
        default=None, alias="marketplaceName", min_length=1, max_length=256
    )


class DshPluginInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source: str = Field(min_length=1, max_length=2048)
    accept_host_permissions: bool = Field(default=False, alias="acceptHostPermissions")

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("source must be one exact registry package")
        # The authenticated Studio API is a production-facing package install
        # boundary.  Local paths remain available to the CLI/developer bridge,
        # where they are packed into the immutable store; exposing arbitrary
        # host paths to a browser request would turn the API into a file reader.
        if Path(normalized).expanduser().is_absolute():
            raise ValueError(
                "Studio only accepts registry packages; use the local CLI for development"
            )
        return validate_dsh_registry_source(normalized)


class DshPluginUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    accept_host_permissions: bool = Field(default=False, alias="acceptHostPermissions")


class DshUiSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    plugin_id: str = Field(alias="pluginId", min_length=1, max_length=256)
    client_digest: str = Field(
        alias="clientDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    tool_ids: list[str] = Field(default_factory=list, alias="toolIds", max_length=256)
    agent_id: str | None = Field(default=None, alias="agentId", min_length=1, max_length=256)

    @field_validator("tool_ids")
    @classmethod
    def validate_tool_ids(cls, value: list[str]) -> list[str]:
        if any(
            not item
            or len(item) > 128
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", item) is None
            for item in value
        ):
            raise ValueError("toolIds contains an invalid DSH tool name")
        if len(value) != len(set(value)):
            raise ValueError("toolIds must be unique")
        return value


class DshUiRelayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    source_id: str = Field(
        alias="sourceId", pattern=r"^frame_[A-Za-z0-9_-]{16,96}$"
    )
    frame_origin: Literal["null"] = Field(default="null", alias="frameOrigin")
    message: dict[str, Any]


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
    detail: CodexPluginDetail,
    *,
    host: CodexBridgeHost,
    home_mode: str,
    snapshot: CodexWorkspacePluginSnapshot | None = None,
) -> dict[str, Any]:
    payload = {
        "item": _public_codex_inventory(detail.inventory, host=host, home_mode=home_mode),
        "description": detail.description,
        "capabilities": {
            "skills": list(detail.skills),
            "mcpServers": list(detail.mcp_servers),
            "hooks": list(detail.hooks),
            "apps": list(detail.apps),
            "scheduledTasks": list(detail.scheduled_tasks),
        },
        "snapshot": None,
        "snapshotRequired": detail.inventory.installed,
    }
    if snapshot is not None:
        projection = _public_codex_snapshot(snapshot)
        payload["snapshot"] = projection
        payload["snapshotRequired"] = False
        payload["item"].update(
            snapshotDigest=projection["snapshotDigest"],
            pluginRef=projection["pluginRef"],
            components=projection["components"],
        )
    return payload


_EXACT_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\."
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_FULL_GIT_COMMIT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _require_exact_codex_version(inventory: CodexPluginInventory) -> str:
    version = str(inventory.version or "")
    if _EXACT_SEMVER.fullmatch(version) is None:
        raise StudioError(
            "CODEX_PLUGIN_SOURCE_NOT_IMMUTABLE",
            "Codex 插件缺少可锁定的精确语义版本",
            status_code=422,
            details={
                "pluginId": inventory.plugin_id,
                "sourceType": inventory.source.type,
                "version": inventory.version,
            },
        )
    return version


def _codex_source_coordinate(inventory: CodexPluginInventory) -> CodexPluginSourceCoordinate:
    raw = inventory.source.model_dump(by_alias=True, exclude_none=True, mode="json")
    source_type = str(raw.get("type") or "remote")
    marketplace = inventory.marketplace_name
    version = _require_exact_codex_version(inventory)
    integrity: str | None = None
    if source_type == "local":
        requested = str(raw.get("path") or inventory.name)
        resolved = f"codex-marketplace://{marketplace}/{inventory.name}@{version}"
    elif source_type == "git":
        requested = str(raw.get("url") or inventory.name)
        revision = str(raw.get("sha") or "")
        if _FULL_GIT_COMMIT.fullmatch(revision) is None:
            raise StudioError(
                "CODEX_PLUGIN_SOURCE_NOT_IMMUTABLE",
                "Git Codex 插件必须由宿主解析为完整 commit SHA 后才能提交快照",
                status_code=422,
                details={
                    "pluginId": inventory.plugin_id,
                    "refName": raw.get("refName"),
                    "sha": raw.get("sha"),
                },
            )
        resolved = f"{requested.rstrip('/')}@{revision.lower()}"
    elif source_type == "npm":
        package = str(raw.get("package") or inventory.name)
        source_version = str(raw.get("version") or "")
        if _EXACT_SEMVER.fullmatch(source_version) is None or source_version != version:
            raise StudioError(
                "CODEX_PLUGIN_SOURCE_NOT_IMMUTABLE",
                "npm Codex 插件必须由宿主解析为一致的精确版本后才能提交快照",
                status_code=422,
                details={
                    "pluginId": inventory.plugin_id,
                    "requestedVersion": raw.get("version"),
                    "resolvedVersion": inventory.version,
                },
            )
        requested = f"{package}@{source_version}"
        resolved = requested
        raw_integrity = raw.get("integrity")
        integrity = str(raw_integrity) if raw_integrity else None
    else:
        requested = inventory.plugin_id
        resolved = f"codex-marketplace://{marketplace}/{inventory.name}@{version}"
    return CodexPluginSourceCoordinate(
        type=source_type,
        requested=requested,
        resolved=resolved,
        marketplace_name=marketplace,
        registry=raw.get("registry"),
        integrity=integrity,
    )


def _observe_codex_snapshot(
    codex_home: Path,
    inventory: CodexPluginInventory,
) -> CodexInstalledPluginSnapshot:
    if not inventory.installed:
        raise StudioError(
            "CODEX_PLUGIN_NOT_INSTALLED",
            "Codex 插件尚未安装，无法提交不可变快照",
            status_code=409,
            details={"pluginId": inventory.plugin_id},
        )
    installed_root = find_installed_codex_plugin_root(
        codex_home,
        marketplace_name=inventory.marketplace_name,
        plugin_name=inventory.name,
        version=inventory.version,
    )
    observed = snapshot_installed_codex_plugin(
        installed_root,
        source=_codex_source_coordinate(inventory),
    )
    if observed.manifest.version != inventory.version:
        raise StudioError(
            "CODEX_PLUGIN_VERSION_MISMATCH",
            "Codex 宿主清单版本与已安装插件清单不一致",
            status_code=409,
            details={
                "pluginId": inventory.plugin_id,
                "hostVersion": inventory.version,
                "manifestVersion": observed.manifest.version,
            },
        )
    return observed


def _lookup_codex_snapshot(
    studio: StudioService,
    codex_home: Path,
    inventory: CodexPluginInventory,
) -> CodexWorkspacePluginSnapshot | None:
    if not inventory.installed:
        return None
    try:
        observed = _observe_codex_snapshot(codex_home, inventory)
    except (StudioError, OSError, ValueError):
        # GET is an inventory operation.  Invalid/unpinned host bytes remain
        # visible but require the explicit admission POST, which reports the
        # actionable validation failure.
        return None
    return studio.codex_plugin_snapshots.lookup(observed)


def _commit_codex_snapshot(
    studio: StudioService,
    codex_home: Path,
    inventory: CodexPluginInventory,
) -> CodexWorkspacePluginSnapshot:
    return studio.codex_plugin_snapshots.commit(
        _observe_codex_snapshot(codex_home, inventory)
    )


def _snapshot_failure_details(error: Exception) -> dict[str, Any]:
    if isinstance(error, StudioError):
        return {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        }
    return {"code": type(error).__name__, "message": str(error)}


def _public_codex_snapshot(snapshot: CodexWorkspacePluginSnapshot) -> dict[str, Any]:
    return {
        "snapshotDigest": snapshot.snapshot_digest,
        "pluginRef": snapshot.plugin_ref,
        "artifactDigest": snapshot.artifact_digest,
        "manifestDigest": snapshot.manifest_digest,
        "components": [
            {
                "id": component_selector(component),
                "kind": component.kind,
                "name": component.name,
                "path": component.path,
                "digest": component.content_digest,
            }
            for component in snapshot.components
        ],
    }


def _public_dsh_inventory(
    inventory: DshPluginInventory,
    *,
    host: DshBridgeHost,
    home_mode: str,
    runtime_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = inventory.client_bundle
    if inventory.source_digest is not None:
        source = {
            "type": "local-immutable",
            "name": inventory.name,
            "kind": inventory.source_kind,
            "integrity": inventory.source_digest,
        }
    else:
        source = {
            "type": "registry",
            "package": inventory.name,
            "requested": inventory.requested_spec,
            "resolvedVersion": inventory.version,
        }
    return {
        "ecosystem": "dsh",
        "integrationMode": "bridged",
        "pluginId": inventory.name,
        "resolvedVersion": inventory.version,
        "distributionName": inventory.name,
        "displayName": inventory.display_name,
        "description": inventory.description,
        "profile": inventory.profile,
        "source": source,
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


def _public_dsh_client_bundle(
    inventory: DshPluginInventory,
    *,
    allow_legacy_top_level: bool = False,
) -> dict[str, Any] | None:
    client = inventory.client_bundle
    if client is None:
        return None
    sandbox_compatible, sandbox_reason = _dsh_sandbox_client_compatibility(inventory)
    query = urlencode({"pluginName": inventory.name, "digest": client.digest})
    execution = select_dsh_client_bundle_execution(
        sandbox_compatible=sandbox_compatible,
        legacy_compatible=client.compatible,
        explicit_legacy_opt_in=allow_legacy_top_level,
    )
    return {
        "pluginId": inventory.name,
        "enabled": inventory.enabled,
        # The source-free legacy Studio loader understands only this field and
        # executes compatible bundles in the authenticated top-level window.
        # Keep it false unless a trusted caller adds an explicit legacy gate.
        "compatible": execution == DshClientBundleExecution.LEGACY_TOP_LEVEL,
        "sandboxCompatible": sandbox_compatible,
        "executionMode": execution.value,
        "digest": client.digest,
        "contentBytes": client.content_bytes,
        "external": list(client.external),
        "inject": list(client.inject),
        "incompatibilityReason": client.incompatibility_reason or None,
        "sandboxIncompatibilityReason": sandbox_reason,
        "url": (
            f"/api/v1/plugin-ecosystems/dsh/client-bundle?{query}"
            if execution == DshClientBundleExecution.LEGACY_TOP_LEVEL
            else None
        ),
        "sandboxBundleUrl": (
            f"{DSH_UI_SANDBOX_BUNDLE_PATH}?{query}" if sandbox_compatible else None
        ),
    }


def _dsh_sandbox_client_compatibility(
    inventory: DshPluginInventory,
) -> tuple[bool, str | None]:
    """Accept only bundles that need no host module graph inside the opaque frame."""

    client = inventory.client_bundle
    if client is None:
        return False, "plugin does not declare a web client bundle"
    if not client.compatible:
        return False, client.incompatibility_reason or "client bundle is not compatible"
    if client.external or client.inject:
        return (
            False,
            "sandbox client bundle must be self-contained (external and inject must be empty)",
        )
    return True, None


def _codex_error(error: Exception) -> StudioError:
    if isinstance(error, StudioError):
        return error
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

    async def cancel_ui_calls(
        session_calls: dict[str, tuple[str, ...]],
    ) -> None:
        internal_ids = (
            dsh_ui_mcp_call_id(session_id, call_id)
            for session_id, call_ids in session_calls.items()
            for call_id in call_ids
        )
        await asyncio.gather(
            *(studio.dsh_capabilities.cancel(call_id) for call_id in internal_ids),
            return_exceptions=True,
        )

    async def purge_expired_ui_sessions() -> None:
        await cancel_ui_calls(studio.dsh_ui_sessions.purge_expired())

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
                snapshot = _lookup_codex_snapshot(studio, codex_home, detail.inventory)
                return _public_codex_detail(
                    detail,
                    host=bridge.host,
                    home_mode=codex_mode,
                    snapshot=snapshot,
                )
        except Exception as error:
            raise _codex_error(error) from None

    @app.post("/api/v1/plugin-ecosystems/codex/plugins/{plugin_id}:snapshot")
    async def snapshot_codex_plugin(plugin_id: str, payload: CodexPluginSnapshotRequest):
        """Explicitly admit already-installed host bytes into the workspace store."""

        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                detail = await bridge.read_plugin(
                    plugin_id,
                    marketplace_name=payload.marketplace_name,
                )
                snapshot = _commit_codex_snapshot(studio, codex_home, detail.inventory)
                projection = _public_codex_snapshot(snapshot)
                item = _public_codex_inventory(
                    detail.inventory,
                    host=bridge.host,
                    home_mode=codex_mode,
                )
                item.update(
                    snapshotDigest=projection["snapshotDigest"],
                    pluginRef=projection["pluginRef"],
                    components=projection["components"],
                )
                return {"item": item, "snapshot": projection, "snapshotRequired": False}
        except Exception as error:
            raise _codex_error(error) from None

    @app.post("/api/v1/plugin-ecosystems/codex/plugins/{plugin_id}:install")
    async def install_codex_plugin(plugin_id: str, payload: CodexPluginInstallRequest):
        if not payload.accept_undeclared_permissions:
            raise _codex_error(CodexPluginApprovalRequired("approval required"))
        try:
            async with CodexAppServerPluginBridge(codex_home=codex_home) as bridge:
                before = await bridge.read_plugin(
                    plugin_id,
                    marketplace_name=payload.marketplace_name,
                )
                result = await bridge.install_plugin(
                    plugin_id,
                    marketplace_name=payload.marketplace_name,
                    accept_undeclared_permissions=True,
                    install_attempt_id=f"studio-{uuid4().hex}",
                )
                try:
                    snapshot = _commit_codex_snapshot(studio, codex_home, result.inventory)
                except Exception as snapshot_error:
                    failure = _snapshot_failure_details(snapshot_error)
                    inventory = _public_codex_inventory(
                        result.inventory,
                        host=bridge.host,
                        home_mode=codex_mode,
                    )
                    if before.inventory.installed:
                        raise StudioError(
                            "CODEX_PLUGIN_INSTALLED_BUT_UNADMITTED",
                            "Codex 插件仍由宿主安装，但未能提交 KsADK 不可变快照",
                            status_code=409,
                            details={
                                "inventory": inventory,
                                "snapshotFailure": failure,
                                "compensation": "not-attempted-preexisting-install",
                            },
                        ) from snapshot_error
                    try:
                        await bridge.uninstall_plugin(result.inventory.plugin_id)
                    except Exception as rollback_error:
                        raise StudioError(
                            "CODEX_PLUGIN_INSTALL_RECONCILIATION_REQUIRED",
                            "Codex 插件快照提交失败，且宿主卸载补偿未能确认",
                            status_code=409,
                            details={
                                "inventory": inventory,
                                "snapshotFailure": failure,
                                "compensation": "uninstall-unconfirmed",
                                "compensationFailure": {
                                    "type": type(rollback_error).__name__,
                                    "message": str(rollback_error),
                                },
                            },
                        ) from rollback_error
                    raise StudioError(
                        "CODEX_PLUGIN_SNAPSHOT_FAILED_ROLLED_BACK",
                        "Codex 插件快照提交失败；本次新安装已由宿主卸载补偿",
                        status_code=409,
                        details={
                            "pluginId": result.inventory.plugin_id,
                            "marketplaceName": result.inventory.marketplace_name,
                            "snapshotFailure": failure,
                            "compensation": "uninstalled",
                        },
                    ) from snapshot_error
                snapshot_projection = _public_codex_snapshot(snapshot)
                item = _public_codex_inventory(
                    result.inventory, host=bridge.host, home_mode=codex_mode
                )
                if snapshot_projection is not None:
                    item.update(
                        snapshotDigest=snapshot_projection["snapshotDigest"],
                        pluginRef=snapshot_projection["pluginRef"],
                        components=snapshot_projection["components"],
                    )
                return {
                    "item": item,
                    "snapshot": snapshot_projection,
                    "snapshotRequired": False,
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

    @app.get(DSH_UI_SANDBOX_BUNDLE_PATH)
    async def get_dsh_sandbox_client_bundle(
        plugin_name: str = Query(alias="pluginName", min_length=1, max_length=256),
        digest: str = Query(pattern=r"^sha256:[0-9a-f]{64}$"),
    ):
        """Serve code only, anonymously, after rechecking the enabled digest fence."""

        _, item_and_content = await asyncio.to_thread(
            call_dsh,
            lambda bridge: (
                bridge.get_plugin(plugin_name),
                bridge.read_client_bundle(plugin_name, expected_digest=digest),
            ),
        )
        item, content = item_and_content
        if not item.enabled:
            raise StudioError(
                "DSH_UI_CLIENT_UNAVAILABLE",
                "DSH UI client 未启用，不能在 sandbox 中执行",
                status_code=409,
            )
        sandbox_compatible, _reason = _dsh_sandbox_client_compatibility(item)
        if not sandbox_compatible:
            raise StudioError(
                "DSH_UI_CLIENT_NOT_SELF_CONTAINED",
                "DSH UI client 依赖宿主模块图，不能在独立 sandbox 中执行",
                status_code=409,
            )
        return Response(content=content, headers=dict(dsh_ui_client_bundle_headers(digest)))

    @app.get(DSH_UI_SANDBOX_FRAME_PATH)
    async def get_dsh_sandbox_frame(
        ui_session_id: str = Query(
            alias="uiSessionId", pattern=r"^dshui_[A-Za-z0-9_-]{24,96}$"
        ),
    ):
        """Return a token-free opaque-origin shell for one live UI session."""

        await purge_expired_ui_sessions()
        grant = studio.dsh_ui_sessions.frame_grant(ui_session_id)
        bundle_query = urlencode(
            {"pluginName": grant.plugin_id, "digest": grant.client_digest}
        )
        document = render_dsh_ui_sandbox_document(
            grant,
            client_bundle_url=f"{DSH_UI_SANDBOX_BUNDLE_PATH}?{bundle_query}",
            title=f"{grant.plugin_id} extension",
            limits=studio.dsh_ui_sessions.limits,
        )
        return Response(content=document.html, headers=dict(document.response_headers))

    @app.get("/api/v1/plugin-ecosystems/dsh/capabilities")
    async def get_dsh_capabilities():
        snapshot, resource = await studio.dsh_capability_catalog_snapshot()
        descriptor = snapshot.descriptor
        tools = snapshot.tools
        inventory = snapshot.inventory
        return {
            "ecosystem": "dsh",
            "profile": descriptor.profile,
            "profileDigest": descriptor.profile_digest,
            "descriptorDigest": descriptor.descriptor_digest,
            "inventoryDigest": descriptor.inventory_digest,
            "state": inventory.model_dump(by_alias=True, mode="json"),
            "tools": [tool.model_dump(by_alias=True, mode="json") for tool in tools],
            "bindableResource": resource.model_dump(
                by_alias=True, exclude_none=True, mode="json"
            ),
            "uiExtensionContract": {
                "format": "agentkit.dsh-ui-extension/v1",
                "rendering": "sandboxed-iframe",
                "protocolVersion": DSH_UI_PROTOCOL_VERSION,
                "messageMethods": ["listTools", "callTool", "cancelTool"],
                "contributionTypes": [
                    "studio.sidebar.navigation",
                    "studio.route",
                    "studio.workspace.tab",
                ],
                "payloadKind": "declarative-metadata-only",
            },
        }

    async def revoke_ui_session(ui_session_id: str) -> tuple[str, ...]:
        calls = studio.dsh_ui_sessions.revoke_session(ui_session_id)
        await cancel_ui_calls({ui_session_id: calls})
        return calls

    @app.post("/api/v1/plugin-ecosystems/dsh/ui-sessions", status_code=201)
    async def create_dsh_ui_session(payload: DshUiSessionCreateRequest, request: Request):
        await purge_expired_ui_sessions()
        async with studio.dsh_profile_read_transaction():
            host, item_and_content = await asyncio.to_thread(
                call_dsh,
                lambda bridge: (
                    bridge.get_plugin(payload.plugin_id),
                    bridge.read_client_bundle(
                        payload.plugin_id,
                        expected_digest=payload.client_digest,
                    ),
                ),
            )
            item, _content = item_and_content
            sandbox_compatible, _reason = _dsh_sandbox_client_compatibility(item)
            if (
                not item.enabled
                or not sandbox_compatible
                or item.client_bundle is None
                or item.client_bundle.digest != payload.client_digest
            ):
                raise StudioError(
                    "DSH_UI_CLIENT_UNAVAILABLE",
                    "DSH UI client 未启用、不可兼容或摘要已变化",
                    status_code=409,
                )
            descriptor, generation_id = (
                await studio.dsh_capabilities.descriptor_generation()
            )
            descriptor_tools = {tool.name: tool for tool in descriptor.tools}
            allowed_ids = tuple(
                sorted(tool_id for tool_id in payload.tool_ids if tool_id in descriptor_tools)
            )
            extension_hash = hashlib.sha256(
                f"{item.name}\0{payload.client_digest}".encode("utf-8")
            ).hexdigest()[:20]
            extension_id = f"dsh.ui.{extension_hash}"
            extension_path = f"/extensions/dsh/{extension_hash}"
            request_origin = request.headers.get("Origin") or (
                f"{request.url.scheme}://{request.url.netloc}"
            )
            grant = studio.dsh_ui_sessions.create_session(
                plugin_id=item.name,
                extension_id=extension_id,
                client_digest=payload.client_digest,
                descriptor_digest=descriptor.descriptor_digest,
                generation_id=generation_id,
                parent_origin=request_origin,
                allowed_tool_ids=allowed_ids,
                agent_id=payload.agent_id,
            )
        frame_url = f"{DSH_UI_SANDBOX_FRAME_PATH}?{urlencode({'uiSessionId': grant.session_id})}"
        return {
            "uiSessionId": grant.session_id,
            "sourceId": grant.source_id,
            "expiresInSeconds": grant.expires_in_seconds,
            "protocolVersion": grant.protocol_version,
            "descriptorDigest": descriptor.descriptor_digest,
            "inventoryDigest": descriptor.inventory_digest,
            "allowedTools": [
                descriptor_tools[tool_id].model_dump(by_alias=True, mode="json")
                for tool_id in allowed_ids
            ],
            "handshake": grant.host_handshake(),
            "frame": {
                "url": frame_url,
                "sandbox": "allow-scripts",
                "referrerPolicy": "no-referrer",
                "credentialless": True,
            },
            "extensionPoints": [
                {
                    "type": "studio.sidebar.navigation",
                    "id": f"{extension_id}.navigation",
                    "label": item.display_name,
                    "path": extension_path,
                },
                {
                    "type": "studio.route",
                    "id": f"{extension_id}.route",
                    "path": extension_path,
                    "workspaceTabId": extension_id,
                },
                {
                    "type": "studio.workspace.tab",
                    "id": extension_id,
                    "label": item.display_name,
                    "renderer": {
                        "type": "sandboxed-iframe",
                        "frameUrl": frame_url,
                    },
                },
            ],
            "host": _public_host("dsh", host, home_mode=dsh_mode),
        }

    @app.post("/api/v1/plugin-ecosystems/dsh/ui-sessions/{ui_session_id}/messages")
    async def relay_dsh_ui_message(
        ui_session_id: str,
        payload: DshUiRelayRequest,
        request: Request,
    ):
        await purge_expired_ui_sessions()
        request_origin = request.headers.get("Origin") or (
            f"{request.url.scheme}://{request.url.netloc}"
        )
        message = payload.message
        if message.get("sessionId") != ui_session_id:
            raise StudioError(
                "DSH_UI_SESSION_INVALID",
                "DSH UI 会话无效、已过期或来源不匹配",
                status_code=403,
            )
        authorized = studio.dsh_ui_sessions.authorize_message(
            message,
            parent_origin=request_origin,
            source_id=payload.source_id,
            frame_origin=payload.frame_origin,
        )
        descriptor, generation_id = await studio.dsh_capabilities.descriptor_generation()
        if (
            descriptor.descriptor_digest != authorized.descriptor_digest
            or generation_id != authorized.generation_id
        ):
            await revoke_ui_session(ui_session_id)
            raise StudioError(
                "DSH_UI_DESCRIPTOR_CHANGED",
                "DSH capability descriptor 已变化，请重新打开插件界面",
                status_code=409,
            )

        def success(result: Any) -> dict[str, Any]:
            return DshUiSuccessResponse(
                session_id=ui_session_id,
                request_id=authorized.request_id,
                result=result,
            ).model_dump(by_alias=True, mode="json")

        def failure(error: StudioError) -> dict[str, Any]:
            code = error.code if re.fullmatch(r"[A-Z0-9_]{1,128}", error.code) else "DSH_UI_ERROR"
            return DshUiErrorResponse(
                session_id=ui_session_id,
                request_id=authorized.request_id,
                error=DshUiResponseError(code=code, message=error.message[:1024]),
            ).model_dump(by_alias=True, mode="json")

        if authorized.method == "listTools":
            try:
                tools = await studio.dsh_capabilities.list_tools(
                    expected_descriptor_digest=authorized.descriptor_digest,
                    expected_generation_id=authorized.generation_id,
                )
            except StudioError as error:
                if error.code == "DSH_CAPABILITY_GENERATION_CHANGED":
                    await revoke_ui_session(ui_session_id)
                    raise StudioError(
                        "DSH_UI_DESCRIPTOR_CHANGED",
                        "DSH capability descriptor 已变化，请重新打开插件界面",
                        status_code=409,
                    ) from error
                return failure(error)
            allowed = set(authorized.allowed_tool_ids)
            return success(
                {
                    "tools": [
                        tool.model_dump(by_alias=True, mode="json")
                        for tool in tools
                        if tool.name in allowed
                    ]
                }
            )
        internal_call_id = dsh_ui_mcp_call_id(
            ui_session_id,
            authorized.call_id or "missing",
        )
        if authorized.method == "cancelTool":
            cancelled = await studio.dsh_capabilities.cancel(internal_call_id)
            return success({"cancelled": cancelled})
        try:
            try:
                result = await studio.dsh_capabilities.call_tool(
                    call_id=internal_call_id,
                    tool_name=authorized.tool_id or "",
                    arguments=authorized.arguments or {},
                    deadline_ms=authorized.deadline_ms or 30_000,
                    expected_descriptor_digest=authorized.descriptor_digest,
                    expected_generation_id=authorized.generation_id,
                )
                return success(result)
            except StudioError as error:
                if error.code == "DSH_CAPABILITY_GENERATION_CHANGED":
                    await revoke_ui_session(ui_session_id)
                    raise StudioError(
                        "DSH_UI_DESCRIPTOR_CHANGED",
                        "DSH capability descriptor 已变化，请重新打开插件界面",
                        status_code=409,
                    ) from error
                return failure(error)
        finally:
            if authorized.call_id is not None:
                studio.dsh_ui_sessions.complete_call(
                    ui_session_id,
                    authorized.call_id,
                )

    @app.delete("/api/v1/plugin-ecosystems/dsh/ui-sessions/{ui_session_id}", status_code=204)
    async def revoke_dsh_ui_session(ui_session_id: str):
        await purge_expired_ui_sessions()
        await revoke_ui_session(ui_session_id)
        return Response(status_code=204)

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins:install", status_code=201)
    async def install_dsh_plugin(payload: DshPluginInstallRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))

        async def install():  # type: ignore[no-untyped-def]
            return await asyncio.to_thread(
                call_dsh,
                lambda bridge: bridge.install_plugin(
                    payload.source, accept_host_permissions=True
                ),
            )

        host, item = await studio.reconfigure_dsh_profile(install)
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

        if operation == "get":
            return await asyncio.to_thread(call_dsh, action)

        async def mutate():  # type: ignore[no-untyped-def]
            return await asyncio.to_thread(call_dsh, action)

        return await studio.reconfigure_dsh_profile(mutate)

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:enable")
    async def enable_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "enable")
        return {"item": public_dsh(item, host)}

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:disable")
    async def disable_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "disable")
        return {"item": public_dsh(item, host)}

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}:update")
    async def update_dsh_plugin(plugin_name: str, payload: DshPluginUpdateRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))

        async def update():  # type: ignore[no-untyped-def]
            return await asyncio.to_thread(
                call_dsh,
                lambda bridge: bridge.update_plugin(
                    plugin_name, accept_host_permissions=True
                ),
            )

        host, item = await studio.reconfigure_dsh_profile(update)
        return {"item": public_dsh(item, host)}

    @app.get("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}")
    async def get_dsh_plugin(plugin_name: str):
        host, item = await mutate_dsh(plugin_name, "get")
        return {"item": public_dsh(item, host)}

    @app.delete("/api/v1/plugin-ecosystems/dsh/plugins/{plugin_name:path}", status_code=204)
    async def uninstall_dsh_plugin(plugin_name: str):
        await mutate_dsh(plugin_name, "uninstall")
        return Response(status_code=204)


__all__ = [
    "CodexPluginInstallRequest",
    "CodexPluginSnapshotRequest",
    "DSH_UI_SANDBOX_BUNDLE_PATH",
    "DSH_UI_SANDBOX_FRAME_PATH",
    "DshPluginInstallRequest",
    "DshPluginUpdateRequest",
    "DshUiRelayRequest",
    "DshUiSessionCreateRequest",
    "register_plugin_routes",
]
