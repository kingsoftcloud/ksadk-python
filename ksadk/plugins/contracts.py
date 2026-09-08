"""Frozen source contracts and internal records for plugin composition.

Only the models exported through ``__all__`` are public Phase 2 contracts.
``PluginManifest`` and its supporting models are internal admission records:
they project DSH/Codex host inventory into the Python composition engine and
are not a third installable package format or a stable public contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class PluginContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_PLUGIN_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFINITION = re.compile(r"^[a-z][a-z0-9._-]*/v[1-9][0-9]*$")
_SENSITIVE_KEY = re.compile(r"(?:secret|password|token|api[_-]?key)", re.IGNORECASE)
_SECRET_REF_PREFIXES = ("secret://", "env://", "credential://", "vault://")


def _validate_semver(value: str, *, field: str) -> str:
    if not _SEMVER.fullmatch(value):
        raise ValueError(f"{field} must use an exact semantic version")
    return value


def _validate_digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError("digest must be a lowercase sha256: digest")
    return value


def _validate_secret_references(value: Any, *, path: str = "config") -> None:
    """Configuration may carry references but never clear secret material."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _SENSITIVE_KEY.search(key_text) and child is not None:
                if not isinstance(child, str) or not child.startswith(_SECRET_REF_PREFIXES):
                    raise ValueError(f"{child_path} must contain a secret reference, not a value")
            _validate_secret_references(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_secret_references(child, path=f"{path}[{index}]")


class PluginMetadata(PluginContractModel):
    id: str = Field(min_length=3, max_length=128)
    version: str

    @field_validator("id")
    @classmethod
    def validate_plugin_id(cls, value: str) -> str:
        if not _PLUGIN_ID.fullmatch(value):
            raise ValueError("plugin id must be lowercase dot/dash/underscore qualified")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_semver(value, field="plugin version")


class CapabilityOffer(PluginContractModel):
    definition: str
    slot: str = Field(min_length=3, max_length=128)
    mode: Literal["unique", "multiple"]

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: str) -> str:
        if not _DEFINITION.fullmatch(value):
            raise ValueError(
                "definition must be a versioned capability name such as agent.provider/v1"
            )
        return value


class PluginSourceSnapshot(PluginContractModel):
    """Credential-free upstream identity retained in an immutable plugin lock.

    Lifecycle hosts (Codex App Server or DSH) remain responsible for resolving
    and installing packages.  This record only captures the exact source that
    was admitted so a later build cannot silently follow a mutable marketplace,
    Git branch, npm tag, or local directory.
    """

    ecosystem: Literal["ksadk", "codex", "dsh"]
    type: Literal["builtin", "registry", "local", "git", "npm", "market", "runtime-native"]
    requested: str = Field(min_length=1, max_length=2048)
    resolved: str = Field(min_length=1, max_length=2048)
    integrity: str | None = Field(default=None, max_length=512)
    marketplace: str | None = Field(default=None, max_length=256)
    registry: str | None = Field(default=None, max_length=2048)

    @field_validator("requested", "resolved", "integrity", "marketplace", "registry")
    @classmethod
    def validate_public_coordinate(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("plugin source coordinates must be trimmed printable text")
        # Build artifacts and inventories are routinely shared.  A source URL
        # therefore cannot retain embedded credentials, query tokens, or URL
        # fragments.  Authentication remains a host-owned credential concern.
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("plugin source coordinates must not contain credentials")
        return value


class LockedPluginComponent(PluginContractModel):
    """One selected native component and the bytes admitted for a build."""

    id: str = Field(min_length=1, max_length=256)
    kind: Literal["skill", "mcp", "hook", "app", "client"]
    digest: str
    path: str | None = Field(default=None, max_length=512)

    @field_validator("id")
    @classmethod
    def validate_component_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
            raise ValueError("plugin component id is invalid")
        return value

    @field_validator("digest")
    @classmethod
    def validate_component_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("path")
    @classmethod
    def validate_component_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\\" in value:
            raise ValueError("plugin component paths must use POSIX separators")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("plugin component path must stay relative to the plugin root")
        return value


class CapabilityRequirement(PluginContractModel):
    definition: str
    version: str = Field(min_length=1, max_length=128)

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: str) -> str:
        if not _DEFINITION.fullmatch(value):
            raise ValueError("definition must be a versioned capability name")
        return value


class PluginCompatibility(PluginContractModel):
    kernel_api: str = Field(min_length=1, max_length=128)
    runtime_protocols: list[str] = Field(default_factory=list)
    python: str | None = Field(default=None, max_length=128)
    platforms: list[str] = Field(default_factory=list)


class PluginProvenance(PluginContractModel):
    source: Literal["builtin", "registry", "local", "market", "runtime-native"]
    digest: str
    signature_ref: str | None = Field(default=None, max_length=512)
    license: str | None = Field(default=None, max_length=128)
    upstream: PluginSourceSnapshot | None = None
    components: tuple[LockedPluginComponent, ...] | None = None

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("components")
    @classmethod
    def validate_components(
        cls, value: tuple[LockedPluginComponent, ...] | None
    ) -> tuple[LockedPluginComponent, ...] | None:
        if value is None:
            return None
        identities = [(item.kind, item.id) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("plugin components must have unique kind/id identities")
        return tuple(sorted(value, key=lambda item: (item.kind, item.id, item.digest)))


class PluginSpec(PluginContractModel):
    domain: Literal["ksadk-platform", "runtime-native"]
    runtime: Literal["python", "node", "process", "remote", "native"]
    entrypoint: str | None = Field(default=None, max_length=512)
    provides: list[CapabilityOffer] = Field(min_length=1)
    requires: list[CapabilityRequirement] = Field(default_factory=list)
    optional: list[CapabilityRequirement] = Field(default_factory=list)
    config_schema: str | None = Field(default=None, max_length=512)
    secret_fields: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    isolation: Literal["in-process", "process", "sidecar", "remote", "native"]
    compatibility: PluginCompatibility
    health_contract: str = Field(min_length=1, max_length=128)
    provenance: PluginProvenance

    @model_validator(mode="after")
    def validate_shape(self) -> "PluginSpec":
        if self.runtime == "native" and self.domain != "runtime-native":
            raise ValueError("native runtime plugins must use runtime-native domain")
        if self.runtime != "native" and not self.entrypoint:
            raise ValueError("non-native plugins require an entrypoint")
        offers = [(item.definition, item.slot) for item in self.provides]
        if len(offers) != len(set(offers)):
            raise ValueError("plugin provides duplicate capability/slot offers")
        if len(self.secret_fields) != len(set(self.secret_fields)):
            raise ValueError("plugin secretFields must be unique")
        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("plugin permissions must be unique")
        return self


class PluginManifest(PluginContractModel):
    """Internal host projection; never a developer-authored plugin package."""

    api_version: Literal["plugin.ksadk.io/v1"] = "plugin.ksadk.io/v1"
    kind: Literal["Plugin"] = "Plugin"
    metadata: PluginMetadata
    spec: PluginSpec


class CapabilityDefinitionSpec(PluginContractModel):
    definition: str
    slot: str = Field(min_length=3, max_length=128)
    multiplicity: Literal["unique", "multiple"]
    owner_required: bool = True
    config_schema: str | None = Field(default=None, max_length=512)

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: str) -> str:
        if not _DEFINITION.fullmatch(value):
            raise ValueError("definition must be a versioned capability name")
        return value


class CapabilityDefinition(PluginContractModel):
    api_version: Literal["capability.ksadk.io/v1"] = "capability.ksadk.io/v1"
    kind: Literal["CapabilityDefinition"] = "CapabilityDefinition"
    metadata: PluginMetadata
    spec: CapabilityDefinitionSpec


class PluginReference(PluginContractModel):
    ref: str = Field(min_length=12, max_length=256)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if not value.startswith("plugin://") or "@" not in value:
            raise ValueError("plugin reference must be plugin://<id>@<exact-version>")
        plugin_id, version = value.removeprefix("plugin://").rsplit("@", 1)
        if not _PLUGIN_ID.fullmatch(plugin_id):
            raise ValueError("plugin reference id is invalid")
        _validate_semver(version, field="plugin reference version")
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_secret_references(value)
        return value


class CompositionCapability(PluginContractModel):
    ref: str = Field(min_length=8, max_length=512)
    required: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        if not value.startswith(("plugin://", "mcp://", "skill://")) or "@" not in value:
            raise ValueError(
                "capability ref must be a pinned plugin://, mcp://, or skill:// reference"
            )
        return value

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_secret_references(value)
        return value


class NativeExtension(PluginContractModel):
    runtime: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=8, max_length=512)


class CompositionProfile(PluginContractModel):
    api_version: Literal["composition.ksadk.io/v1"] = "composition.ksadk.io/v1"
    agent_provider: PluginReference
    capabilities: list[CompositionCapability] = Field(default_factory=list)
    native_extensions: list[NativeExtension] = Field(default_factory=list)
    policies: dict[str, str] = Field(default_factory=dict)
    ui_contributions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_profile(self) -> "CompositionProfile":
        refs = [item.ref for item in self.capabilities]
        if len(refs) != len(set(refs)):
            raise ValueError("composition capabilities must not repeat a reference")
        extensions = [(item.runtime, item.ref) for item in self.native_extensions]
        if len(extensions) != len(set(extensions)):
            raise ValueError("native extensions must not repeat a runtime/reference pair")
        return self


class LockedCapability(PluginContractModel):
    definition: str
    slot: str = Field(min_length=3, max_length=128)
    owner: str = Field(min_length=3, max_length=128)

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: str) -> str:
        if not _DEFINITION.fullmatch(value):
            raise ValueError("definition must be a versioned capability name")
        return value


class PluginDependency(PluginContractModel):
    id: str = Field(min_length=3, max_length=128)
    version: str
    digest: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PLUGIN_ID.fullmatch(value):
            raise ValueError("dependency id is invalid")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_semver(value, field="dependency version")

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)


class PluginLockEntry(PluginContractModel):
    id: str = Field(min_length=3, max_length=128)
    version: str
    digest: str
    source: Literal["builtin", "registry", "local", "market", "runtime-native"]
    signature_ref: str | None = Field(default=None, max_length=512)
    license: str | None = Field(default=None, max_length=128)
    provides: list[LockedCapability] = Field(default_factory=list)
    dependencies: list[PluginDependency] = Field(default_factory=list)
    upstream: PluginSourceSnapshot | None = None
    components: tuple[LockedPluginComponent, ...] | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PLUGIN_ID.fullmatch(value):
            raise ValueError("plugin lock id is invalid")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_semver(value, field="plugin lock version")

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("components")
    @classmethod
    def validate_components(
        cls, value: tuple[LockedPluginComponent, ...] | None
    ) -> tuple[LockedPluginComponent, ...] | None:
        if value is None:
            return None
        identities = [(item.kind, item.id) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("plugin lock components must have unique kind/id identities")
        return tuple(sorted(value, key=lambda item: (item.kind, item.id, item.digest)))

    @model_validator(mode="after")
    def validate_external_snapshot(self) -> "PluginLockEntry":
        if self.upstream is not None:
            compatible_sources = {
                "builtin": {"builtin"},
                "registry": {"registry", "npm"},
                "local": {"local", "git", "npm"},
                "market": {"market", "local", "git", "npm"},
                "runtime-native": {"runtime-native"},
            }
            if self.upstream.type not in compatible_sources[self.source]:
                raise ValueError("plugin lock source and upstream source type are inconsistent")
        return self


class PluginLock(PluginContractModel):
    """The current Bundle v2 lock shape, now with an exact typed entry form."""

    lock_format: Literal["agentkit.plugin-lock/v1"] = "agentkit.plugin-lock/v1"
    plugins: list[PluginLockEntry] = Field(default_factory=list)

    @field_validator("plugins")
    @classmethod
    def sort_plugins(cls, value: list[PluginLockEntry]) -> list[PluginLockEntry]:
        return sorted(value, key=lambda item: (item.id, item.version, item.digest))

    @model_validator(mode="after")
    def validate_graph(self) -> "PluginLock":
        by_id = {item.id: item for item in self.plugins}
        if len(by_id) != len(self.plugins):
            raise ValueError("plugin lock must pin exactly one version for each plugin id")
        for item in self.plugins:
            for dependency in item.dependencies:
                target = by_id.get(dependency.id)
                if target is None:
                    raise ValueError(f"plugin dependency {dependency.id!r} is not pinned")
                if (target.version, target.digest) != (dependency.version, dependency.digest):
                    raise ValueError(
                        f"plugin dependency {dependency.id!r} does not match its lock entry"
                    )
                if dependency.id == item.id:
                    raise ValueError("plugin lock cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(plugin_id: str) -> None:
            if plugin_id in visiting:
                raise ValueError("plugin lock dependency cycle")
            if plugin_id in visited:
                return
            visiting.add(plugin_id)
            for dependency in by_id[plugin_id].dependencies:
                visit(dependency.id)
            visiting.remove(plugin_id)
            visited.add(plugin_id)

        for plugin_id in by_id:
            visit(plugin_id)
        return self


class PluginInventoryItem(PluginContractModel):
    id: str = Field(min_length=3, max_length=128)
    version: str
    digest: str
    state: Literal[
        "resolved",
        "admitted",
        "staged",
        "starting",
        "ready",
        "degraded",
        "failed",
        "draining",
        "stopped",
        "disposed",
        "rejected",
    ]
    health: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    reason: str | None = Field(default=None, max_length=1024)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _PLUGIN_ID.fullmatch(value):
            raise ValueError("plugin inventory id is invalid")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_semver(value, field="plugin inventory version")

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return _validate_digest(value)


class PluginInventory(PluginContractModel):
    api_version: Literal["plugin.ksadk.io/v1"] = "plugin.ksadk.io/v1"
    kind: Literal["PluginInventory"] = "PluginInventory"
    profile_digest: str = Field(min_length=8, max_length=80)
    plugin_lock_digest: str = Field(min_length=8, max_length=80)
    plugins: list[PluginInventoryItem] = Field(default_factory=list)

    @field_validator("profile_digest", "plugin_lock_digest")
    @classmethod
    def validate_optional_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("plugins")
    @classmethod
    def sort_plugins(cls, value: list[PluginInventoryItem]) -> list[PluginInventoryItem]:
        return sorted(value, key=lambda item: item.id)


def canonical_plugin_lock(lock: PluginLock) -> bytes:
    """Canonical JSON bytes used by Bundle v2 and admission fingerprinting."""

    return json.dumps(
        lock.model_dump(by_alias=True, exclude_none=True, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def plugin_lock_digest(lock: PluginLock) -> str:
    return f"sha256:{hashlib.sha256(canonical_plugin_lock(lock)).hexdigest()}"


__all__ = [
    "CapabilityDefinition",
    "CompositionProfile",
    "LockedPluginComponent",
    "PluginLock",
    "PluginLockEntry",
    "PluginInventory",
    "PluginInventoryItem",
    "PluginSourceSnapshot",
    "canonical_plugin_lock",
    "plugin_lock_digest",
]
