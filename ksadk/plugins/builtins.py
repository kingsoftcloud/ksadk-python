"""Built-in capability runtimes for immutable AgentBundle execution.

These plugins deliberately consume only the already validated
``ResolvedPluginBundle`` plus explicit process services supplied at host
construction.  They never reach back into Studio's mutable catalogs.  The
module also keeps rendering as a projection of the canonical ConversationItem
contract; it does not introduce another event or transcript pipeline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ksadk.conversations.contracts import ConversationItem
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.harness.config import McpToolSpec
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import (
    CompositionProfile,
    PluginManifest,
    plugin_lock_digest,
)
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.harness import HarnessSkillContribution, HarnessTurnRequest
from ksadk.plugins.resolver import composition_profile_digest
from ksadk.sessions.base import BaseSessionService
from ksadk.sessions.local_service import LocalSessionService

BUILTIN_PLUGIN_VERSION = "1.0.0"

SQLITE_SESSION_STORE_PLUGIN_ID = "io.ksadk.session-store.sqlite"
WORKSPACE_MCP_PLUGIN_ID = "io.ksadk.mcp.workspace"
WORKSPACE_SKILL_PLUGIN_ID = "io.ksadk.skill.workspace"
READ_ONLY_CONTEXT_PLUGIN_ID = "io.ksadk.context.bundle-readonly"
CORE_RENDERER_PLUGIN_ID = "io.ksadk.renderer.conversation-core"

SecretResolver = Callable[[str], str | None]


def _manifest_digest(plugin_id: str) -> str:
    payload = f"{plugin_id}@{BUILTIN_PLUGIN_VERSION}:builtin-runtime-v1".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    mode: str,
    permissions: Sequence[str] = (),
    secret_fields: Sequence[str] = (),
) -> PluginManifest:
    return cast(
        PluginManifest,
        PluginManifest.model_validate(
            {
                "metadata": {"id": plugin_id, "version": BUILTIN_PLUGIN_VERSION},
                "spec": {
                    "domain": "ksadk-platform",
                    "runtime": "python",
                    "entrypoint": "ksadk.plugins.builtins:builtin_capability_factories",
                    "provides": [
                        {"definition": definition, "slot": slot, "mode": mode}
                    ],
                    "secretFields": list(secret_fields),
                    "permissions": list(permissions),
                    "isolation": "in-process",
                    "compatibility": {
                        "kernelApi": ">=1,<2",
                        "runtimeProtocols": ["agentkit.runtime/v1"],
                        "python": ">=3.10,<3.15",
                    },
                    "healthContract": "plugin.health/v1",
                    "provenance": {
                        "source": "builtin",
                        "digest": _manifest_digest(plugin_id),
                        "license": "Apache-2.0",
                    },
                },
            },
        ),
    )


def builtin_capability_manifests() -> tuple[PluginManifest, ...]:
    """Return the exact, deterministic Phase 2 built-in capability catalog."""

    return (
        _manifest(
            SQLITE_SESSION_STORE_PLUGIN_ID,
            definition="session.event-store/v1",
            slot="session.events",
            mode="unique",
            permissions=("filesystem:session-store",),
        ),
        _manifest(
            WORKSPACE_MCP_PLUGIN_ID,
            definition="mcp.connector/v1",
            slot="mcp.workspace",
            mode="multiple",
            permissions=("network:mcp",),
            secret_fields=("apiKeyRef",),
        ),
        _manifest(
            WORKSPACE_SKILL_PLUGIN_ID,
            definition="skill.source/v1",
            slot="skill.workspace",
            mode="multiple",
            permissions=("filesystem:bundle-read",),
        ),
        _manifest(
            READ_ONLY_CONTEXT_PLUGIN_ID,
            definition="context.contributor/v1",
            slot="context.bundle",
            mode="multiple",
            permissions=("filesystem:bundle-read",),
        ),
        _manifest(
            CORE_RENDERER_PLUGIN_ID,
            definition="session.item.renderer/v1",
            slot="renderer.core",
            mode="multiple",
        ),
    )


class _BuiltinRuntime:
    def __init__(self, plugin_id: str, version: str) -> None:
        self.plugin_id = plugin_id
        self.version = version
        self._ready = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError(
                "builtin_capability_disposed",
                f"built-in capability {self.plugin_id}@{self.version} is disposed",
            )
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        self._disposed = True

    def _require_ready(self) -> None:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "builtin_capability_unavailable",
                f"built-in capability {self.plugin_id}@{self.version} is not ready",
            )

    def _config(self, bundle: ResolvedPluginBundle) -> Mapping[str, Any]:
        self._require_ready()
        return _bound_capability_config(bundle, self.plugin_id, self.version)


class SQLiteSessionStoreRuntime(_BuiltinRuntime):
    """Own one durable SQLite session service for an active profile."""

    def __init__(self, plugin_id: str, version: str, *, db_path: Path) -> None:
        super().__init__(plugin_id, version)
        self.db_path = db_path.resolve()
        self._service: LocalSessionService | None = None
        self._event_store: SessionServiceEventStore | None = None

    async def start(self) -> None:
        if self._disposed:
            await super().start()
        try:
            self._service = LocalSessionService(self.db_path)
            self._event_store = SessionServiceEventStore(self._service)
        except Exception as error:  # noqa: BLE001 - durable backend boundary
            self._service = None
            self._event_store = None
            raise PluginHostError(
                "builtin_session_store_start_failed",
                "SQLite session store could not be initialized",
            ) from error
        self._ready = True

    def session_service_for(self, bundle: ResolvedPluginBundle) -> BaseSessionService:
        self._config(bundle)
        if self._service is None:  # pragma: no cover - protected by lifecycle
            raise PluginHostError(
                "builtin_capability_unavailable", "SQLite session service is unavailable"
            )
        return self._service

    def event_store_for(self, bundle: ResolvedPluginBundle) -> SessionServiceEventStore:
        self._config(bundle)
        if self._event_store is None:  # pragma: no cover - protected by lifecycle
            raise PluginHostError(
                "builtin_capability_unavailable", "SQLite event store is unavailable"
            )
        return self._event_store

    async def dispose(self) -> None:
        service = self._service
        self._ready = False
        self._disposed = True
        if service is not None:
            await service.aclose()


class WorkspaceMCPRuntime(_BuiltinRuntime):
    """Materialize locked workspace MCP resources for the Harness provider."""

    def __init__(
        self,
        plugin_id: str,
        version: str,
        *,
        secret_resolver: SecretResolver | None,
    ) -> None:
        super().__init__(plugin_id, version)
        self._secret_resolver = secret_resolver

    def inventory(self, bundle: ResolvedPluginBundle) -> tuple[dict[str, Any], ...]:
        resources = self._resources(bundle)
        inventory: list[dict[str, Any]] = []
        for materializer, resolved in resources:
            inventory.append(
                {
                    "name": resolved["name"],
                    "version": resolved["version"],
                    "transport": resolved["transport"],
                    "endpointUrl": resolved.get("endpointUrl"),
                    "toolFilter": tuple(_string_list(materializer.get("toolFilter"))),
                }
            )
        return tuple(inventory)

    def harness_mcp_specs(
        self, bundle: ResolvedPluginBundle
    ) -> tuple[McpToolSpec, ...]:
        specs: list[McpToolSpec] = []
        for materializer, resolved in self._resources(bundle):
            transport = _required_string(resolved, "transport", code="builtin_mcp_invalid")
            if transport not in {"http", "sse"}:
                raise PluginHostError(
                    "builtin_mcp_transport_unsupported",
                    f"workspace MCP {resolved['name']!r} uses unsupported transport",
                )
            endpoint = _required_string(
                resolved, "endpointUrl", code="builtin_mcp_endpoint_missing"
            )
            secret_ref = _optional_string(materializer.get("apiKeyRef"))
            api_key: str | None = None
            if secret_ref is not None:
                if not _is_secret_reference(secret_ref):
                    raise PluginHostError(
                        "builtin_mcp_secret_ref_invalid",
                        "workspace MCP apiKeyRef must be an external secret reference",
                    )
                if self._secret_resolver is None:
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential resolver is unavailable",
                    )
                try:
                    api_key = self._secret_resolver(secret_ref)
                except Exception as error:  # noqa: BLE001 - secret-provider boundary
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential could not be resolved",
                    ) from error
                if not api_key:
                    raise PluginHostError(
                        "builtin_mcp_secret_unavailable",
                        "workspace MCP credential could not be resolved",
                    )
            specs.append(
                McpToolSpec(
                    name=_required_string(resolved, "name", code="builtin_mcp_invalid"),
                    url=endpoint,
                    api_key=api_key,
                    tool_filter=tuple(_string_list(materializer.get("toolFilter"))),
                    tool_name_prefix=_optional_string(
                        materializer.get("toolNamePrefix")
                    ),
                )
            )
        return tuple(specs)

    def _resources(
        self, bundle: ResolvedPluginBundle
    ) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
        config = self._config(bundle)
        declared = _resource_entries(config, expected_kind="mcp")
        resolved = _resolved_resources(bundle, "mcpServers")
        return tuple(
            (
                _mapping(entry.get("materializer"), code="builtin_mcp_invalid"),
                _match_resource(entry, resolved, code="builtin_mcp_resource_mismatch"),
            )
            for entry in declared
        )


class WorkspaceSkillRuntime(_BuiltinRuntime):
    """Expose integrity-checked Skill instructions copied into the Bundle."""

    def harness_skill(self, bundle: ResolvedPluginBundle) -> HarnessSkillContribution:
        config = self._config(bundle)
        declared = _resource_entries(config, expected_kind="skill")
        resolved = _resolved_resources(bundle, "skills")
        contributions: list[HarnessSkillContribution] = []
        for entry in declared:
            item = _match_resource(
                entry, resolved, code="builtin_skill_resource_mismatch"
            )
            name = _required_string(item, "name", code="builtin_skill_invalid")
            bundle_path = _required_string(
                item, "bundlePath", code="builtin_skill_path_invalid"
            )
            directory = _safe_bundle_path(
                bundle,
                bundle_path,
                code="builtin_skill_path_invalid",
                require_declared=False,
            )
            if not directory.is_dir() or directory.is_symlink():
                raise PluginHostError(
                    "builtin_skill_path_invalid", f"Skill {name!r} is not a Bundle directory"
                )
            expected_digest = _required_string(
                item, "digest", code="builtin_skill_invalid"
            )
            if _directory_digest(directory) != expected_digest:
                raise PluginHostError(
                    "builtin_skill_digest_mismatch",
                    f"Skill {name!r} does not match its locked Bundle digest",
                )
            instructions = _required_string(
                item, "instructions", code="builtin_skill_instructions_missing"
            )
            contributions.append(
                HarnessSkillContribution(name=name, instructions=instructions)
            )
        if not contributions:
            raise PluginHostError(
                "builtin_skill_resource_missing", "workspace Skill has no locked resource"
            )
        if len(contributions) == 1:
            return contributions[0]
        return HarnessSkillContribution(
            name="workspace-skills",
            instructions="\n\n".join(
                f"Skill {item.name}:\n{item.instructions}" for item in contributions
            ),
        )


class ReadOnlyBundleContextRuntime(_BuiltinRuntime):
    """Read declared UTF-8 context files from the immutable Bundle only."""

    async def harness_context(
        self, bundle: ResolvedPluginBundle, request: HarnessTurnRequest
    ) -> str:
        del request
        config = self._config(bundle)
        paths = _string_list(config.get("paths"))
        max_chars = config.get("maxChars", 32_000)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            raise PluginHostError(
                "builtin_context_config_invalid", "maxChars must be a positive integer"
            )
        chunks: list[str] = []
        remaining = max_chars
        for relative in paths:
            path = _safe_bundle_path(
                bundle,
                relative,
                code="builtin_context_path_invalid",
                require_declared=True,
            )
            if not path.is_file() or path.is_symlink():
                raise PluginHostError(
                    "builtin_context_path_invalid",
                    f"Bundle context path is not a regular file: {relative}",
                )
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as error:
                raise PluginHostError(
                    "builtin_context_read_failed",
                    f"Bundle context file could not be read: {relative}",
                ) from error
            if not text or remaining == 0:
                continue
            chunk = text[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
        return "\n\n".join(chunks)


class CoreConversationRendererRuntime(_BuiltinRuntime):
    """Project canonical items into a safe core renderer view model."""

    _COMPONENTS = {
        "user_message": "markdown",
        "assistant_text": "markdown",
        "reasoning": "reasoning",
        "tool_call": "tool",
        "approval": "approval",
        "progress": "progress",
        "plan": "plan",
        "goal": "goal",
        "artifact": "artifact",
        "a2ui": "a2ui",
        "error": "error",
    }

    def render(self, item: ConversationItem) -> dict[str, Any]:
        base: dict[str, Any] = {
            "component": self._COMPONENTS.get(item.kind, "unknown"),
            "itemId": item.item_id,
            "lifecycle": item.lifecycle,
        }
        if item.kind in {"user_message", "assistant_text"}:
            text = item.payload.get("text", "")
            base["text"] = text if isinstance(text, str) else str(text)
            return base
        if item.kind == "unknown":
            base["schemaRef"] = item.payload_schema_ref
            base["summary"] = json.dumps(
                item.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )[:4096]
            return base
        # Structured payload stays data.  Consumers choose trusted local UI
        # components; no Bundle-provided HTML or JavaScript is executed here.
        base["payload"] = dict(item.payload)
        base["schemaRef"] = item.payload_schema_ref
        return base


class SQLiteSessionStoreFactory:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root.resolve()
        self.runtime: SQLiteSessionStoreRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> SQLiteSessionStoreRuntime:
        del services
        _require_manifest(manifest, SQLITE_SESSION_STORE_PLUGIN_ID)
        profile_key = composition_profile_digest(profile).removeprefix("sha256:")
        runtime = SQLiteSessionStoreRuntime(
            manifest.metadata.id,
            manifest.metadata.version,
            db_path=self._state_root / profile_key / "sessions.sqlite",
        )
        self.runtime = runtime
        return runtime


class _WorkspaceMCPFactory:
    def __init__(self, secret_resolver: SecretResolver | None) -> None:
        self._secret_resolver = secret_resolver

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> WorkspaceMCPRuntime:
        del profile
        _require_manifest(manifest, WORKSPACE_MCP_PLUGIN_ID)
        resolver = self._secret_resolver
        service_resolver = services.get("secret_resolver")
        if resolver is None and callable(service_resolver):
            resolver = service_resolver
        return WorkspaceMCPRuntime(
            manifest.metadata.id,
            manifest.metadata.version,
            secret_resolver=resolver,
        )


class _SimpleFactory:
    def __init__(self, plugin_id: str, runtime_type: type[_BuiltinRuntime]) -> None:
        self._plugin_id = plugin_id
        self._runtime_type = runtime_type

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> _BuiltinRuntime:
        del profile, services
        _require_manifest(manifest, self._plugin_id)
        return self._runtime_type(manifest.metadata.id, manifest.metadata.version)


def builtin_capability_factories(
    *,
    state_root: Path,
    secret_resolver: SecretResolver | None = None,
) -> dict[str, Any]:
    """Create factories with explicit state and secret-provider boundaries."""

    return {
        SQLITE_SESSION_STORE_PLUGIN_ID: SQLiteSessionStoreFactory(state_root),
        WORKSPACE_MCP_PLUGIN_ID: _WorkspaceMCPFactory(secret_resolver),
        WORKSPACE_SKILL_PLUGIN_ID: _SimpleFactory(
            WORKSPACE_SKILL_PLUGIN_ID, WorkspaceSkillRuntime
        ),
        READ_ONLY_CONTEXT_PLUGIN_ID: _SimpleFactory(
            READ_ONLY_CONTEXT_PLUGIN_ID, ReadOnlyBundleContextRuntime
        ),
        CORE_RENDERER_PLUGIN_ID: _SimpleFactory(
            CORE_RENDERER_PLUGIN_ID, CoreConversationRendererRuntime
        ),
    }


def _require_manifest(manifest: PluginManifest, plugin_id: str) -> None:
    if (
        manifest.metadata.id != plugin_id
        or manifest.metadata.version != BUILTIN_PLUGIN_VERSION
    ):
        raise PluginHostError(
            "builtin_manifest_mismatch",
            f"factory cannot stage {manifest.metadata.id}@{manifest.metadata.version}",
        )


def _bound_capability_config(
    bundle: ResolvedPluginBundle, plugin_id: str, version: str
) -> Mapping[str, Any]:
    composition = bundle.composition
    if composition_profile_digest(composition.profile) != composition.profile_digest:
        raise PluginHostError(
            "plugin_bundle_profile_mutated",
            "resolved Bundle composition profile changed after validation",
        )
    if plugin_lock_digest(composition.plugin_lock) != composition.plugin_lock_digest:
        raise PluginHostError(
            "plugin_bundle_lock_mutated",
            "resolved Bundle plugin lock changed after validation",
        )
    ref = f"plugin://{plugin_id}@{version}"
    matches = [item for item in composition.profile.capabilities if item.ref == ref]
    if len(matches) != 1:
        raise PluginHostError(
            "builtin_capability_binding_missing",
            f"resolved Bundle does not bind built-in capability {plugin_id}@{version}",
        )
    if not any(
        item.id == plugin_id and item.version == version
        for item in composition.plugin_lock.plugins
    ):
        raise PluginHostError(
            "builtin_capability_lock_missing",
            f"resolved Bundle does not lock built-in capability {plugin_id}@{version}",
        )
    return cast(Mapping[str, Any], matches[0].config)


def _resource_entries(
    config: Mapping[str, Any], *, expected_kind: str
) -> tuple[Mapping[str, Any], ...]:
    raw = config.get("resources")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PluginHostError(
            f"builtin_{expected_kind}_config_invalid", "resources must be an array"
        )
    entries: list[Mapping[str, Any]] = []
    for item in raw:
        entry = _mapping(item, code=f"builtin_{expected_kind}_config_invalid")
        if entry.get("kind") != expected_kind:
            raise PluginHostError(
                f"builtin_{expected_kind}_resource_mismatch",
                f"resource kind must be {expected_kind!r}",
            )
        for field in ("resourceId", "name", "version", "digest"):
            _required_string(entry, field, code=f"builtin_{expected_kind}_invalid")
        entries.append(entry)
    return tuple(entries)


def _resolved_resources(
    bundle: ResolvedPluginBundle, field: str
) -> tuple[Mapping[str, Any], ...]:
    capabilities = _mapping(
        bundle.resolved_agent_spec.get("capabilities"),
        code="builtin_bundle_capabilities_missing",
    )
    raw = capabilities.get(field)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PluginHostError(
            "builtin_bundle_capabilities_missing", f"Bundle capabilities.{field} is missing"
        )
    return tuple(
        _mapping(item, code="builtin_bundle_capabilities_invalid") for item in raw
    )


def _match_resource(
    declared: Mapping[str, Any],
    resolved: Sequence[Mapping[str, Any]],
    *,
    code: str,
) -> Mapping[str, Any]:
    name = _required_string(declared, "name", code=code)
    version = _required_string(declared, "version", code=code)
    digest = _required_string(declared, "digest", code=code)
    matches = [
        item
        for item in resolved
        if item.get("name") == name
        and item.get("version") == version
        and item.get("digest") == digest
    ]
    if len(matches) != 1:
        raise PluginHostError(
            code,
            f"locked resource {name!r}@{version} is missing from resolved Agent spec",
        )
    return matches[0]


def _safe_bundle_path(
    bundle: ResolvedPluginBundle,
    relative_text: str,
    *,
    code: str,
    require_declared: bool,
) -> Path:
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PluginHostError(code, f"unsafe Bundle path: {relative_text}")
    path = bundle.root.joinpath(*relative.parts)
    cursor = bundle.root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PluginHostError(code, f"Bundle path cannot use symlinks: {relative_text}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(bundle.root)
    except (OSError, ValueError) as error:
        raise PluginHostError(code, f"Bundle path is unavailable: {relative_text}") from error
    if require_declared and relative.as_posix() not in {
        item.path for item in bundle.manifest.files
    }:
        raise PluginHostError(code, f"Bundle path is not declared: {relative_text}")
    return cast(Path, resolved)


def _directory_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise PluginHostError(
                "builtin_skill_path_invalid", "Skill Bundle cannot contain symlinks"
            )
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _mapping(value: Any, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PluginHostError(code, "expected an object")
    return value


def _required_string(value: Mapping[str, Any], field: str, *, code: str) -> str:
    text = value.get(field)
    if not isinstance(text, str) or not text.strip():
        raise PluginHostError(code, f"{field} must be a non-empty string")
    return text.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PluginHostError(
            "builtin_config_invalid", "optional configuration value must be a string"
        )
    return value.strip() or None


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHostError("builtin_config_invalid", "configuration value must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PluginHostError(
                "builtin_config_invalid", "array entries must be non-empty strings"
            )
        result.append(item.strip())
    return tuple(result)


def _is_secret_reference(value: str) -> bool:
    return value.startswith(("secret://", "env://", "credential://", "vault://"))


__all__ = [
    "BUILTIN_PLUGIN_VERSION",
    "CORE_RENDERER_PLUGIN_ID",
    "READ_ONLY_CONTEXT_PLUGIN_ID",
    "SQLITE_SESSION_STORE_PLUGIN_ID",
    "WORKSPACE_MCP_PLUGIN_ID",
    "WORKSPACE_SKILL_PLUGIN_ID",
    "CoreConversationRendererRuntime",
    "ReadOnlyBundleContextRuntime",
    "SQLiteSessionStoreFactory",
    "SQLiteSessionStoreRuntime",
    "WorkspaceMCPRuntime",
    "WorkspaceSkillRuntime",
    "builtin_capability_factories",
    "builtin_capability_manifests",
]
