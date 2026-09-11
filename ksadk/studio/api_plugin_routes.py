"""Studio lifecycle API for DSH plugins and the Codex compatibility bridge."""

from __future__ import annotations

import asyncio
import base64
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
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
    DshProfileRecoveryError,
    validate_dsh_registry_request,
)
from ksadk.plugins.codex_manifest import (
    CodexInstalledPluginSnapshot,
    CodexPluginSourceCoordinate,
    snapshot_installed_codex_plugin,
)
from ksadk.plugins.dsh_home import (
    DshHomeVersionError,
    dsh_home_diagnostic,
    prepare_studio_dsh_home,
    studio_dsh_home,
)
from ksadk.plugins.dsh_toolchain import DshToolchainError, DshToolchainManager
from ksadk.plugins.host import PluginHostError
from ksadk.studio.codex_plugin_store import (
    CodexWorkspacePluginSnapshot,
    component_selector,
    find_installed_codex_plugin_root,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


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
        return validate_dsh_registry_request(normalized)


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
    home = studio_dsh_home(studio.workspace.root)
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
    profile = os.environ.get("KSADK_DSH_PROFILE", "").strip() or "web"
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
    presentation = _public_plugin_interface(inventory.interface)
    if not presentation.get("logoUrl"):
        artwork = _local_plugin_artwork(inventory)
        if artwork:
            presentation["logoUrl"] = artwork
    return {
        "ecosystem": "codex",
        "integrationMode": "bridged",
        "pluginId": inventory.plugin_id,
        "resolvedVersion": inventory.version,
        "distributionName": inventory.name,
        "displayName": inventory.interface.get("displayName") or inventory.name,
        "description": inventory.interface.get("shortDescription"),
        "interface": presentation,
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


def _local_plugin_artwork(inventory: CodexPluginInventory) -> str | None:
    """Read only bounded image assets inside the host-reported local plugin.

    Never expose host paths, accept browser-supplied filenames, or fetch URLs.
    SVGs remain image data (not executable same-origin HTML documents).
    """
    if inventory.source.type != "local":
        return None
    root = Path(inventory.source.path)
    if not root.is_absolute():
        if not inventory.marketplace_path:
            return None
        root = Path(inventory.marketplace_path) / root
    root = root.resolve()
    if not (root / ".codex-plugin" / "plugin.json").is_file():
        return None
    for key in ("logo", "composerIcon"):
        raw = inventory.interface.get(key)
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            with path.open("rb") as stream:
                data = stream.read(256 * 1024 + 1)
        except OSError:
            continue
        if len(data) > 256 * 1024:
            continue
        mime = None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        elif path.suffix.lower() == ".svg" and b"<svg" in data[:1024]:
            mime = "image/svg+xml"
        if mime:
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    return None


def _public_plugin_interface(value: dict[str, Any]) -> dict[str, Any]:
    """Project marketplace display data, never filesystem paths or host state."""
    result = {
        key: value[key] for key in (
            "displayName", "shortDescription", "longDescription", "developerName", "category",
        ) if isinstance(value.get(key), str)
    }
    for key in ("defaultPrompt", "capabilities"):
        if isinstance(value.get(key), list):
            result[key] = [item for item in value[key] if isinstance(item, str)]
    for key in (
        "logoUrl", "logoUrlDark", "composerIconUrl", "websiteUrl",
        "privacyPolicyUrl", "termsOfServiceUrl",
    ):
        raw = value.get(key)
        if isinstance(raw, str) and urlparse(raw).scheme == "https" and not urlparse(raw).username:
            result[key] = raw
    return result


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
    return studio.codex_plugin_snapshots.commit(_observe_codex_snapshot(codex_home, inventory))


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
        "clientExtension": inventory.client_extension,
        "settingsIntegration": inventory.settings_integration,
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
        "host": _public_host("dsh", host, home_mode=home_mode),
    }


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
    if isinstance(error, DshHomeVersionError):
        return StudioError(
            "DSH_HOME_VERSION_UNVERIFIED", str(error), status_code=409,
            details=error.diagnostic,
        )
    if isinstance(error, DshPluginNotFoundError):
        return StudioError("DSH_PLUGIN_NOT_FOUND", "DSH 插件未安装", status_code=404)
    if isinstance(error, DshPluginApprovalRequired):
        return StudioError(
            "DSH_PLUGIN_RISK_CONFIRMATION_REQUIRED",
            "安装或升级 DSH 插件前必须确认宿主权限风险",
            status_code=422,
        )
    if isinstance(error, DshProfileRecoveryError):
        return StudioError(
            "DSH_PROFILE_RECOVERY_REQUIRED",
            "Profile 迁移恢复失败，已保留恢复备份并暂停运行准入",
            status_code=503,
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

    def call_dsh(operation: Callable[[DshProfilePluginBridge], Any], *, write: bool = False) -> Any:
        try:
            # Even a read-only bridge creates its synchronization directory.
            # Fence an empty home first so that lockfile cannot make our own
            # fresh home look like an unversioned legacy installation later.
            if write or dsh_home_diagnostic(dsh_home)["status"] == "new":
                prepare_studio_dsh_home(dsh_home)
            with DshProfilePluginBridge(
                dsh_home=dsh_home, profile=dsh_profile, dsh_command=dsh_command
            ) as bridge:
                return bridge.host, operation(bridge)
        except Exception as error:
            raise _dsh_error(error) from None

    @app.post("/api/v1/plugin-ecosystems/dsh/core/session")
    async def start_dsh_core_session(response: Response):
        """Start the official full Core DSH Web profile on demand.

        The token-bearing URL is returned only from this authenticated,
        CSRF-protected local POST and must never be persisted in Studio state.
        """

        try:
            lease = await studio.dsh_capabilities.connector_lease()
            descriptor = await studio.dsh_capabilities.describe()
            browser_url = lease.browser_url()
        except PluginHostError as error:
            raise StudioError(
                "DSH_CORE_RUNTIME_UNAVAILABLE",
                "完整 DSH Core 当前无法启动",
                status_code=503,
                details={"reason": error.code},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return {
            "protocolVersion": "ksadk.dsh-core-runtime/v1",
            "version": descriptor.dsh_version,
            "profile": descriptor.profile,
            "endpoint": browser_url.split("?", 1)[0],
            "browserUrl": browser_url,
        }

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
                "homeCompatibility": dsh_home_diagnostic(dsh_home, workspace=studio.workspace.root),
            }
        except StudioError as error:
            return {
                "ecosystem": "dsh",
                "integrationMode": "bridged",
                "profile": dsh_profile,
                "host": _public_host("dsh", None, home_mode=dsh_mode),
                "items": [],
                "error": {"code": error.code, "message": error.message},
                "homeCompatibility": dsh_home_diagnostic(dsh_home, workspace=studio.workspace.root),
            }

    @app.get("/api/v1/plugin-ecosystems/dsh/profile")
    async def get_dsh_profile_projection():
        host, projection = await asyncio.to_thread(
            call_dsh, lambda bridge: bridge.project_profile(), write=True,
        )
        return {
            "host": _public_host("dsh", host, home_mode=dsh_mode),
            "profile": projection.model_dump(mode="json", by_alias=True),
        }

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
            "bindableResource": resource.model_dump(by_alias=True, exclude_none=True, mode="json"),
        }

    @app.post("/api/v1/plugin-ecosystems/dsh/profile:migrate-layout")
    async def migrate_dsh_profile_layout(payload: DshPluginUpdateRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))

        def migrate(bridge: DshProfilePluginBridge):
            bridge.migrate_to_isolated_layout(accept_host_permissions=True)
            return bridge.project_profile()

        async def operation():
            # Do not release Studio's admission fence while a cancelled HTTP
            # request still has a filesystem migration running in its thread.
            task = asyncio.create_task(asyncio.to_thread(call_dsh, migrate, write=True))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                # Retrieve any exception; the reconfiguration owner will recover
                # before allowing another run even when the requester has gone.
                error = task.exception()
                if error is not None:
                    raise error
                raise asyncio.CancelledError
            return task.result()

        host, projection = await studio.reconfigure_dsh_profile(operation)
        return {
            "profile": projection.model_dump(mode="json", by_alias=True),
            "nodeLinker": "isolated",
            "host": {"id": host.host_id, "version": host.version, "available": True},
        }

    @app.post("/api/v1/plugin-ecosystems/dsh/plugins:install", status_code=201)
    async def install_dsh_plugin(payload: DshPluginInstallRequest):
        if not payload.accept_host_permissions:
            raise _dsh_error(DshPluginApprovalRequired("approval required"))

        async def install():  # type: ignore[no-untyped-def]
            return await asyncio.to_thread(
                call_dsh,
                lambda bridge: bridge.install_plugin(payload.source, accept_host_permissions=True),
                write=True,
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
            return await asyncio.to_thread(call_dsh, action, write=True)

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
                lambda bridge: bridge.update_plugin(plugin_name, accept_host_permissions=True),
                write=True,
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
    "DshPluginInstallRequest",
    "DshPluginUpdateRequest",
    "register_plugin_routes",
]
