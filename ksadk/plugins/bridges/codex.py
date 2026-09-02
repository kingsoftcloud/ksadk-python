"""Restricted Codex App Server plugin lifecycle bridge.

Codex remains the native owner of Codex plugins.  KsADK only calls the
allowlisted App Server lifecycle methods and projects their inventory into
strict local models.  There is intentionally no arbitrary JSON-RPC escape
hatch and no attempt to execute a Codex plugin inside PluginHost.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _CodexWireModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class _LocalSource(_CodexWireModel):
    type: Literal["local"]
    path: str


class _GitSource(_CodexWireModel):
    type: Literal["git"]
    url: str
    ref_name: str | None = None
    sha: str | None = None
    path: str | None = None


class _NpmSource(_CodexWireModel):
    type: Literal["npm"]
    package: str
    version: str | None = None
    registry: str | None = None


class _RemoteSource(_CodexWireModel):
    type: Literal["remote"]


CodexPluginSource = _LocalSource | _GitSource | _NpmSource | _RemoteSource


class _PluginSummary(_CodexWireModel):
    id: str
    name: str
    source: CodexPluginSource = Field(discriminator="type")
    installed: bool
    enabled: bool
    install_policy: Literal["NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"]
    auth_policy: Literal["ON_INSTALL", "ON_USE"]
    remote_plugin_id: str | None = None
    version: str | None = None
    local_version: str | None = None
    installed_at: int | None = None
    install_policy_source: str | None = None
    must_show_installation_interstitial: bool | None = None
    availability: str = "AVAILABLE"
    disabled_reason: str | None = None
    eligible_plan_types: tuple[str, ...] | None = None
    share_context: dict[str, Any] | None = None
    interface: dict[str, Any] | None = None
    keywords: tuple[str, ...] = ()


class _Marketplace(_CodexWireModel):
    name: str
    path: str | None = None
    interface: dict[str, Any] | None = None
    plugins: tuple[_PluginSummary, ...]


class _MarketplaceLoadError(_CodexWireModel):
    marketplace_path: str
    message: str


class _PluginListResponse(_CodexWireModel):
    marketplaces: tuple[_Marketplace, ...]
    marketplace_load_errors: tuple[_MarketplaceLoadError, ...] = ()
    featured_plugin_ids: tuple[str, ...] = ()


class _PluginDetailWire(_CodexWireModel):
    marketplace_name: str
    marketplace_path: str | None
    summary: _PluginSummary
    description: str | None = None
    share_url: str | None = None
    skills: tuple[dict[str, Any], ...]
    hooks: tuple[dict[str, Any], ...]
    apps: tuple[dict[str, Any], ...]
    app_templates: tuple[dict[str, Any], ...]
    mcp_servers: tuple[str, ...]
    scheduled_tasks: tuple[dict[str, Any], ...] | None = None


class _PluginReadResponse(_CodexWireModel):
    plugin: _PluginDetailWire


class _MarketplaceAddResponse(_CodexWireModel):
    marketplace_name: str
    installed_root: str
    already_added: bool


class _PluginInstallResponse(_CodexWireModel):
    auth_policy: Literal["ON_INSTALL", "ON_USE"]
    apps_needing_auth: tuple[dict[str, Any], ...]


class _PluginUninstallResponse(_CodexWireModel):
    pass


class CodexPluginInventory(_CodexWireModel):
    """Normalized observed state; install receipt never implies these fields."""

    plugin_id: str
    name: str
    marketplace_name: str
    marketplace_path: str | None
    version: str | None
    installed: bool
    enabled: bool
    availability: str
    source: CodexPluginSource = Field(discriminator="type")
    permissions_declared: Literal[False] = False
    risk_disclosures: tuple[str, ...] = (
        "Codex plugin permissions are host-managed and not declared in the plugin manifest.",
        "The Codex App Server and installed plugin run with the current host user privileges.",
    )


class CodexPluginDetail(_CodexWireModel):
    inventory: CodexPluginInventory
    description: str | None = None
    skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    apps: tuple[str, ...] = ()
    scheduled_tasks: tuple[str, ...] = ()


class CodexPluginInstallResult(_CodexWireModel):
    inventory: CodexPluginInventory
    auth_policy: Literal["ON_INSTALL", "ON_USE"]
    apps_needing_auth: tuple[str, ...] = ()


class CodexPluginUninstallResult(_CodexWireModel):
    plugin_id: str
    installed: Literal[False] = False
    enabled: Literal[False] = False


class CodexBridgeHost(_CodexWireModel):
    host_id: Literal["codex-app-server"] = "codex-app-server"
    version: str
    protocol: Literal["codex.app-server/v1"] = "codex.app-server/v1"
    available: Literal[True] = True


class CodexBridgeError(RuntimeError):
    """Base error for a bounded Codex plugin lifecycle operation."""


class CodexPluginNotFoundError(CodexBridgeError):
    pass


class CodexPluginApprovalRequired(CodexBridgeError):
    pass


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class _CodexTransport(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def initialize(self) -> Any: ...

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        response_model: type[ResponseT],
    ) -> ResponseT: ...


_HOST_VERSION = re.compile(r"(?:Codex(?: Desktop)?/)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


def _reported_host_version(metadata: Any) -> str:
    """Return an App Server version without making its user-agent a gate.

    App Server exposes plugin lifecycle methods as the compatibility contract.
    Its ``userAgent`` field is diagnostic metadata and has changed shape across
    CLI, Desktop, and SDK launches.  A missing or unfamiliar value must remain
    visible to callers, but must not prevent an otherwise compatible host from
    listing, installing, or removing a plugin.
    """
    raw = getattr(metadata, "user_agent", None) or getattr(metadata, "userAgent", None)
    if raw is None and isinstance(metadata, dict):
        raw = metadata.get("userAgent") or metadata.get("user_agent")
    if raw is None and isinstance(metadata, BaseModel):
        payload = metadata.model_dump(by_alias=True)
        raw = payload.get("userAgent") or payload.get("user_agent")
    match = _HOST_VERSION.search(str(raw or ""))
    return match.group(1) if match is not None else "unreported"


class CodexAppServerPluginBridge:
    """Allowlisted Codex plugin manager backed by one App Server process."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        codex_bin: str | None = None,
        transport: _CodexTransport | None = None,
    ) -> None:
        if transport is not None and (codex_home is not None or codex_bin is not None):
            raise ValueError("an injected transport cannot be combined with Codex launch options")
        self._codex_home = codex_home
        self._codex_bin = codex_bin
        self._transport = transport
        self._owns_transport = transport is None
        self._started = False
        self._host: CodexBridgeHost | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "CodexAppServerPluginBridge":
        await self.start()
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.close()

    @property
    def host(self) -> CodexBridgeHost:
        if self._host is None:
            raise CodexBridgeError("Codex App Server bridge is not started")
        return self._host

    async def start(self) -> CodexBridgeHost:
        if self._started:
            return self.host
        if self._codex_home is not None:
            self._codex_home.mkdir(parents=True, exist_ok=True)
        if self._transport is None:
            self._transport = self._create_transport()
        try:
            await self._transport.start()
            metadata = await self._transport.initialize()
            self._host = CodexBridgeHost(version=_reported_host_version(metadata))
            self._started = True
            return self._host
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        transport = self._transport
        self._transport = None if self._owns_transport else transport
        self._started = False
        self._host = None
        if transport is not None:
            await transport.close()

    async def add_marketplace(self, source: str, *, ref_name: str | None = None) -> str:
        response = await self._request(
            "marketplace/add",
            {"source": source, "refName": ref_name, "sparsePaths": None},
            _MarketplaceAddResponse,
        )
        return response.marketplace_name

    async def list_plugins(
        self, *, force_refetch: bool = False
    ) -> tuple[CodexPluginInventory, ...]:
        response = await self._list_wire(force_refetch=force_refetch)
        return tuple(
            self._inventory(marketplace, summary)
            for marketplace in response.marketplaces
            for summary in marketplace.plugins
        )

    async def read_plugin(
        self,
        plugin_name_or_id: str,
        *,
        marketplace_name: str | None = None,
    ) -> CodexPluginDetail:
        marketplace, summary = await self._resolve(plugin_name_or_id, marketplace_name)
        response = await self._request(
            "plugin/read",
            {
                "pluginName": summary.name,
                "marketplacePath": marketplace.path,
                "remoteMarketplaceName": None if marketplace.path else marketplace.name,
            },
            _PluginReadResponse,
        )
        plugin = response.plugin
        return CodexPluginDetail(
            inventory=self._inventory(marketplace, plugin.summary),
            description=plugin.description,
            skills=tuple(str(item.get("name", "")) for item in plugin.skills if item.get("name")),
            mcp_servers=plugin.mcp_servers,
            hooks=tuple(str(item.get("key", "")) for item in plugin.hooks if item.get("key")),
            apps=tuple(str(item.get("id", "")) for item in plugin.apps if item.get("id")),
            scheduled_tasks=tuple(
                str(item.get("key", ""))
                for item in (plugin.scheduled_tasks or ())
                if item.get("key")
            ),
        )

    async def install_plugin(
        self,
        plugin_name_or_id: str,
        *,
        marketplace_name: str | None = None,
        accept_undeclared_permissions: bool = False,
        install_attempt_id: str | None = None,
    ) -> CodexPluginInstallResult:
        if not accept_undeclared_permissions:
            raise CodexPluginApprovalRequired(
                "Codex plugins do not expose complete install/runtime permissions; "
                "explicit accept_undeclared_permissions=True is required"
            )
        async with self._lock:
            marketplace, summary = await self._resolve(plugin_name_or_id, marketplace_name)
            before = self._inventory(marketplace, summary)
            try:
                response = await self._request(
                    "plugin/install",
                    {
                        "pluginName": summary.name,
                        "marketplacePath": marketplace.path,
                        "remoteMarketplaceName": None if marketplace.path else marketplace.name,
                        "installAttemptId": install_attempt_id,
                    },
                    _PluginInstallResponse,
                )
                observed = await self._resolve_inventory(
                    summary.id,
                    marketplace.name,
                    force_refetch=True,
                )
                if not observed.installed or not observed.enabled:
                    raise CodexBridgeError(
                        "Codex host did not reconcile the installed plugin as enabled"
                    )
            except BaseException as install_error:
                try:
                    restored = await asyncio.shield(self._restore_failed_install(before))
                except BaseException as rollback_error:
                    raise CodexBridgeError(
                        "Codex plugin install failed and its previous inventory "
                        "could not be restored"
                    ) from rollback_error
                raise CodexBridgeError(
                    "Codex plugin install failed; previous inventory was restored "
                    f"(installed={restored.installed}, enabled={restored.enabled})"
                ) from install_error
            return CodexPluginInstallResult(
                inventory=observed,
                auth_policy=response.auth_policy,
                apps_needing_auth=tuple(
                    str(item.get("id", "")) for item in response.apps_needing_auth if item.get("id")
                ),
            )

    async def uninstall_plugin(self, plugin_id: str) -> CodexPluginUninstallResult:
        async with self._lock:
            marketplace, summary = await self._resolve(plugin_id, None)
            await self._request(
                "plugin/uninstall",
                {"pluginId": summary.id},
                _PluginUninstallResponse,
            )
            observed = await self._resolve_inventory(summary.id, marketplace.name)
            if observed.installed or observed.enabled:
                raise CodexBridgeError("Codex host still reports the uninstalled plugin as active")
            return CodexPluginUninstallResult(plugin_id=summary.id)

    def _create_transport(self) -> _CodexTransport:
        try:
            from openai_codex.async_client import AsyncCodexClient
            from openai_codex.client import CodexConfig
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise CodexBridgeError("Codex plugin bridge requires the ksadk[codex] extra") from exc
        env = None
        if self._codex_home is not None:
            env = {"CODEX_HOME": str(self._codex_home.resolve())}
        return cast(
            _CodexTransport,
            AsyncCodexClient(CodexConfig(codex_bin=self._codex_bin, env=env)),
        )

    async def _request(
        self,
        method: Literal[
            "marketplace/add",
            "plugin/list",
            "plugin/read",
            "plugin/install",
            "plugin/uninstall",
        ],
        params: dict[str, Any] | None,
        response_model: type[ResponseT],
    ) -> ResponseT:
        if not self._started or self._transport is None:
            raise CodexBridgeError("Codex App Server bridge is not started")
        return await self._transport.request(method, params, response_model=response_model)

    async def _list_wire(self, *, force_refetch: bool = False) -> _PluginListResponse:
        return await self._request(
            "plugin/list",
            {"forceRefetch": force_refetch, "cwds": None, "marketplaceKinds": None},
            _PluginListResponse,
        )

    async def _resolve(
        self,
        plugin_name_or_id: str,
        marketplace_name: str | None,
        *,
        force_refetch: bool = False,
    ) -> tuple[_Marketplace, _PluginSummary]:
        response = await self._list_wire(force_refetch=force_refetch)
        matches = [
            (marketplace, summary)
            for marketplace in response.marketplaces
            if marketplace_name is None or marketplace.name == marketplace_name
            for summary in marketplace.plugins
            if summary.id == plugin_name_or_id or summary.name == plugin_name_or_id
        ]
        if len(matches) != 1:
            qualifier = f" in marketplace {marketplace_name!r}" if marketplace_name else ""
            if not matches:
                raise CodexPluginNotFoundError(
                    f"Codex plugin {plugin_name_or_id!r}{qualifier} was not found"
                )
            raise CodexBridgeError(
                f"Codex plugin name {plugin_name_or_id!r} is ambiguous; use its full plugin id"
            )
        return matches[0]

    async def _resolve_inventory(
        self,
        plugin_id: str,
        marketplace_name: str,
        *,
        force_refetch: bool = False,
    ) -> CodexPluginInventory:
        marketplace, summary = await self._resolve(
            plugin_id,
            marketplace_name,
            force_refetch=force_refetch,
        )
        return self._inventory(marketplace, summary)

    async def _restore_failed_install(
        self,
        before: CodexPluginInventory,
    ) -> CodexPluginInventory:
        """Compensate an install whose receipt/reconciliation failed.

        App Server may have committed files before its response is lost.  For
        the only state transition exposed by ``install_plugin`` (available ->
        installed), uninstall is the native compensating action.  An already
        installed plugin is never silently reinstalled because the App Server
        API cannot pin its previous local bytes; any observed drift therefore
        fails closed rather than claiming rollback.
        """

        observed = await self._resolve_inventory(
            before.plugin_id,
            before.marketplace_name,
            force_refetch=True,
        )
        if not before.installed and observed.installed:
            await self._request(
                "plugin/uninstall",
                {"pluginId": before.plugin_id},
                _PluginUninstallResponse,
            )
            observed = await self._resolve_inventory(
                before.plugin_id,
                before.marketplace_name,
                force_refetch=True,
            )
        if (observed.installed, observed.enabled) != (
            before.installed,
            before.enabled,
        ):
            raise CodexBridgeError("Codex host inventory differs from the pre-install snapshot")
        return observed

    @staticmethod
    def _inventory(
        marketplace: _Marketplace,
        summary: _PluginSummary,
    ) -> CodexPluginInventory:
        return CodexPluginInventory(
            plugin_id=summary.id,
            name=summary.name,
            marketplace_name=marketplace.name,
            marketplace_path=marketplace.path,
            version=summary.local_version or summary.version,
            installed=summary.installed,
            enabled=summary.enabled,
            availability=summary.availability,
            source=summary.source,
        )


__all__ = [
    "CodexAppServerPluginBridge",
    "CodexBridgeError",
    "CodexBridgeHost",
    "CodexPluginApprovalRequired",
    "CodexPluginDetail",
    "CodexPluginInstallResult",
    "CodexPluginInventory",
    "CodexPluginNotFoundError",
    "CodexPluginUninstallResult",
]
