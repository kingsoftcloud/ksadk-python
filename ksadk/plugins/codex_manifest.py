"""Parse and snapshot the file-based data plane of a Codex plugin.

Codex App Server remains the lifecycle authority for marketplaces and plugin
installation.  This module starts at the installed plugin root: it validates
the official ``.codex-plugin/plugin.json`` contract, resolves only contained
component paths, and records deterministic content digests for admission into
KsADK's immutable build contracts.

The upstream manifest is allowed to grow new fields.  Fields understood here
remain strictly typed, and spelling aliases are normalized only for the
documented camelCase form and KsADK's legacy snake_case form.  Supplying both
spellings is rejected instead of silently choosing one.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from ksadk.plugins.contracts import LockedPluginComponent, PluginSourceSnapshot

_MANIFEST_RELATIVE_PATH = PurePosixPath(".codex-plugin/plugin.json")
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_ARTIFACT_FILES = 20_000
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\."
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class CodexPluginManifestError(ValueError):
    """The installed Codex plugin data plane is malformed or unsafe."""


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _CodexSnapshotModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )


class _CodexUpstreamModel(_CodexSnapshotModel):
    # Native manifests and companion objects are versioned by Codex, not by
    # KsADK.  Preserve additive fields while validating every field we consume.
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=True,
    )


def _normalize_aliases(
    value: Any,
    aliases: Mapping[str, str],
    *,
    label: str,
) -> Any:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    for snake_name, camel_name in aliases.items():
        if snake_name in normalized and camel_name in normalized:
            raise ValueError(f"{label} cannot contain both {camel_name!r} and {snake_name!r}")
        # Pydantic accepts the declared alias and field name.  Leave the
        # caller's spelling intact so validation errors point at the source
        # document rather than at a rewritten key.
    return normalized


def _non_empty(value: str, *, field: str) -> str:
    if not value or value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} must be trimmed, non-empty printable text")
    return value


def _relative_reference(
    value: str,
    *,
    field: str,
    require_dot_prefix: bool = False,
) -> str:
    _non_empty(value, field=field)
    if "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must use a relative POSIX path")
    if require_dot_prefix and not value.startswith("./"):
        raise ValueError(f"{field} must start with './' relative to the plugin root")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
        raise ValueError(f"{field} must stay relative to the plugin root")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError(f"{field} must name a component inside the plugin root")
    return value


class CodexPluginAuthor(_CodexUpstreamModel):
    name: str
    email: str | None = None
    url: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _non_empty(value, field="author.name")

    @field_validator("email", "url")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, field="author metadata")


class CodexPluginInterface(_CodexUpstreamModel):
    display_name: str | None = Field(default=None, alias="displayName")
    short_description: str | None = Field(default=None, alias="shortDescription")
    long_description: str | None = Field(default=None, alias="longDescription")
    developer_name: str | None = Field(default=None, alias="developerName")
    category: str | None = None
    capabilities: tuple[str, ...] = ()
    website_url: str | None = Field(default=None, alias="websiteURL")
    privacy_policy_url: str | None = Field(default=None, alias="privacyPolicyURL")
    terms_of_service_url: str | None = Field(default=None, alias="termsOfServiceURL")
    default_prompt: str | tuple[str, ...] | None = Field(default=None, alias="defaultPrompt")
    brand_color: str | None = Field(default=None, alias="brandColor")
    composer_icon: str | None = Field(default=None, alias="composerIcon")
    logo: str | None = None
    logo_dark: str | None = Field(default=None, alias="logoDark")
    screenshots: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_spelling(cls, value: Any) -> Any:
        return _normalize_aliases(
            value,
            {
                "display_name": "displayName",
                "short_description": "shortDescription",
                "long_description": "longDescription",
                "developer_name": "developerName",
                "website_url": "websiteURL",
                "privacy_policy_url": "privacyPolicyURL",
                "terms_of_service_url": "termsOfServiceURL",
                "default_prompt": "defaultPrompt",
                "brand_color": "brandColor",
                "composer_icon": "composerIcon",
                "logo_dark": "logoDark",
            },
            label="plugin.json interface",
        )

    @field_validator(
        "display_name",
        "short_description",
        "long_description",
        "developer_name",
        "category",
        "website_url",
        "privacy_policy_url",
        "terms_of_service_url",
        "brand_color",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, field="interface field")

    @field_validator("capabilities", "screenshots")
    @classmethod
    def validate_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _non_empty(item, field="interface list entry")
        return value

    @field_validator("default_prompt")
    @classmethod
    def validate_prompt(cls, value: str | tuple[str, ...] | None) -> str | tuple[str, ...] | None:
        if isinstance(value, str):
            return _non_empty(value, field="interface.defaultPrompt")
        if isinstance(value, tuple):
            for item in value:
                _non_empty(item, field="interface.defaultPrompt entry")
        return value

    @field_validator("composer_icon", "logo", "logo_dark")
    @classmethod
    def validate_optional_asset_path(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _relative_reference(
                value,
                field="interface asset",
                require_dot_prefix=True,
            )
        )

    @field_validator("screenshots")
    @classmethod
    def validate_screenshot_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _relative_reference(
                item,
                field="interface screenshot",
                require_dot_prefix=True,
            )
        return value


class CodexPluginManifest(_CodexUpstreamModel):
    """Normalized official manifest.

    Only ``name`` is universally required by the native format.  Installed
    official plugins exist with deliberately minimal metadata, so optional
    presentation fields cannot be promoted to KsADK-only requirements.
    """

    name: str
    version: str | None = None
    id: str | None = None
    description: str | None = None
    author: CodexPluginAuthor | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: tuple[str, ...] = ()
    skills: str | tuple[str, ...] | None = None
    hooks: (
        str
        | tuple[str, ...]
        | dict[str, Any]
        | tuple[dict[str, Any], ...]
        | None
    ) = None
    mcp_servers: str | dict[str, dict[str, Any]] | None = Field(
        default=None,
        alias="mcpServers",
    )
    apps: str | None = None
    interface: CodexPluginInterface | None = None
    bundled_content_variant: str | None = Field(
        default=None,
        alias="bundledContentVariant",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_spelling(cls, value: Any) -> Any:
        return _normalize_aliases(
            value,
            {
                "mcp_servers": "mcpServers",
                "bundled_content_variant": "bundledContentVariant",
            },
            label="plugin.json",
        )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        _non_empty(value, field="plugin.json name")
        if len(value) > 128 or _PLUGIN_NAME.fullmatch(value) is None:
            raise ValueError("plugin.json name is not a valid Codex plugin identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str | None) -> str | None:
        if value is not None and _SEMVER.fullmatch(value) is None:
            raise ValueError("plugin.json version must be exact semantic version text")
        return value

    @field_validator(
        "id",
        "description",
        "homepage",
        "repository",
        "license",
        "bundled_content_variant",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, field="plugin.json field")

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _non_empty(item, field="plugin.json keyword")
        if len(value) != len(set(value)):
            raise ValueError("plugin.json keywords must be unique")
        return value

    @field_validator("apps")
    @classmethod
    def validate_component_path(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _relative_reference(
                value,
                field="component path",
                require_dot_prefix=True,
            )
        )

    @field_validator("skills", mode="before")
    @classmethod
    def validate_skill_paths(cls, value: Any) -> Any:
        if value is None:
            return None
        paths = [value] if isinstance(value, str) else value
        if not isinstance(paths, (list, tuple)) or not paths or not all(
            isinstance(item, str) for item in paths
        ):
            raise ValueError("skills must be a path or non-empty array of paths")
        normalized = tuple(
            _relative_reference(
                item,
                field="skills",
                require_dot_prefix=True,
            )
            for item in paths
        )
        return normalized[0] if isinstance(value, str) else normalized

    @field_validator("hooks", mode="before")
    @classmethod
    def validate_hook_declaration(cls, value: Any) -> Any:
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            return _relative_reference(
                value,
                field="hooks",
                require_dot_prefix=True,
            )
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(
                "hooks must be a path, path array, inline object, or inline object array"
            )
        if all(isinstance(item, str) for item in value):
            return tuple(
                _relative_reference(
                    item,
                    field="hooks",
                    require_dot_prefix=True,
                )
                for item in value
            )
        if all(isinstance(item, dict) for item in value):
            return tuple(dict(item) for item in value)
        raise ValueError("hooks arrays cannot mix paths and inline hook objects")

    @field_validator("mcp_servers")
    @classmethod
    def validate_mcp_declaration(
        cls, value: str | dict[str, dict[str, Any]] | None
    ) -> str | dict[str, dict[str, Any]] | None:
        if isinstance(value, str):
            return _relative_reference(
                value,
                field="mcpServers",
                require_dot_prefix=True,
            )
        if isinstance(value, dict):
            _validate_named_object_map(value, label="plugin.json mcpServers")
        return value


class CodexPluginSourceCoordinate(_CodexSnapshotModel):
    """Credential-free requested and resolved native source coordinate."""

    coordinate_format: Literal["codex.plugin-source/v1"] = "codex.plugin-source/v1"
    type: Literal["local", "git", "npm", "remote"]
    requested: str = Field(min_length=1, max_length=2048)
    resolved: str = Field(min_length=1, max_length=2048)
    marketplace_name: str | None = Field(default=None, max_length=256)
    registry: str | None = Field(default=None, max_length=2048)
    integrity: str | None = Field(default=None, max_length=512)

    @field_validator("requested", "resolved", "marketplace_name", "registry", "integrity")
    @classmethod
    def validate_coordinate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _non_empty(value, field="source coordinate")
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("source coordinates must not embed credentials or query data")
        return value

    def to_plugin_source_snapshot(self) -> PluginSourceSnapshot:
        """Project into the generic lock model without coupling the parser to it."""

        from ksadk.plugins.contracts import PluginSourceSnapshot

        source_type: Literal["local", "git", "npm", "market"]
        source_type = "market" if self.type == "remote" else self.type
        return PluginSourceSnapshot(
            ecosystem="codex",
            type=source_type,
            requested=self.requested,
            resolved=self.resolved,
            integrity=self.integrity,
            marketplace=self.marketplace_name,
            registry=self.registry,
        )


class CodexPluginComponentSnapshot(_CodexSnapshotModel):
    component_format: Literal["codex.plugin-component/v1"] = "codex.plugin-component/v1"
    kind: Literal["skill", "mcp", "hook", "app"]
    name: str = Field(min_length=1, max_length=256)
    path: str | None = Field(default=None, max_length=1024)
    content_digest: str

    @field_validator("name")
    @classmethod
    def validate_component_name(cls, value: str) -> str:
        return _non_empty(value, field="component name")

    @field_validator("path")
    @classmethod
    def validate_component_path(cls, value: str | None) -> str | None:
        return None if value is None else _relative_reference(value, field="component path")

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("component contentDigest must be a lowercase sha256 digest")
        return value

    def to_locked_component(self) -> LockedPluginComponent:
        from ksadk.plugins.contracts import LockedPluginComponent

        return LockedPluginComponent(
            id=self.name,
            kind=self.kind,
            digest=self.content_digest,
            path=self.path,
        )


class CodexInstalledPluginSnapshot(_CodexSnapshotModel):
    snapshot_format: Literal["codex.installed-plugin-snapshot/v1"] = (
        "codex.installed-plugin-snapshot/v1"
    )
    installed_root: str = Field(min_length=1, max_length=4096)
    source: CodexPluginSourceCoordinate
    manifest: CodexPluginManifest
    manifest_digest: str
    artifact_digest: str
    components: tuple[CodexPluginComponentSnapshot, ...] = ()

    @field_validator("manifest_digest", "artifact_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("snapshot digests must be lowercase sha256 digests")
        return value

    @field_validator("components")
    @classmethod
    def validate_components(
        cls, value: tuple[CodexPluginComponentSnapshot, ...]
    ) -> tuple[CodexPluginComponentSnapshot, ...]:
        identities = [(component.kind, component.name) for component in value]
        if len(identities) != len(set(identities)):
            raise ValueError("component kind/name identities must be unique")
        return tuple(sorted(value, key=lambda item: (item.kind, item.name, item.path or "")))


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json_digest(value: Any) -> str:
    return _sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _validate_root(plugin_root: Path) -> Path:
    root = Path(plugin_root).expanduser()
    if root.is_symlink():
        raise CodexPluginManifestError("installed plugin root must not be a symlink")
    if not root.is_dir():
        raise CodexPluginManifestError("installed plugin root must be an existing directory")
    return root.resolve()


def _contained_path(
    root: Path,
    raw_path: str | PurePosixPath,
    *,
    label: str,
    expected: Literal["file", "directory", "either"] = "either",
) -> Path:
    relative_text = raw_path.as_posix() if isinstance(raw_path, PurePosixPath) else raw_path
    try:
        _relative_reference(relative_text, field=label)
    except ValueError as exc:
        raise CodexPluginManifestError(str(exc)) from exc
    relative = PurePosixPath(relative_text)
    cursor = root
    for part in relative.parts:
        if part == ".":
            continue
        cursor /= part
        if cursor.is_symlink():
            raise CodexPluginManifestError(f"{label} must not traverse a symlink: {relative}")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CodexPluginManifestError(
            f"{label} does not resolve to an existing path inside the plugin root: {relative}"
        ) from exc
    if expected == "file" and not resolved.is_file():
        raise CodexPluginManifestError(f"{label} must resolve to a regular file: {relative}")
    if expected == "directory" and not resolved.is_dir():
        raise CodexPluginManifestError(f"{label} must resolve to a directory: {relative}")
    if expected == "either" and not (resolved.is_file() or resolved.is_dir()):
        raise CodexPluginManifestError(f"{label} must resolve to a regular file or directory")
    return resolved


def _read_json_object(path: Path, *, root: Path, label: str) -> tuple[dict[str, Any], bytes]:
    relative = path.relative_to(root).as_posix()
    path = _contained_path(root, relative, label=label, expected="file")
    try:
        stat = path.stat()
        if stat.st_size > _MAX_JSON_BYTES:
            raise CodexPluginManifestError(f"{label} exceeds {_MAX_JSON_BYTES} bytes")
        raw = path.read_bytes()
        value = json.loads(raw)
    except CodexPluginManifestError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexPluginManifestError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CodexPluginManifestError(f"{label} must contain one JSON object")
    return value, raw


def _validate_named_object_map(value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise CodexPluginManifestError(f"{label} must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for name, config in value.items():
        if not isinstance(name, str):
            raise CodexPluginManifestError(f"{label} names must be strings")
        try:
            _non_empty(name, field=f"{label} name")
        except ValueError as exc:
            raise CodexPluginManifestError(str(exc)) from exc
        if not isinstance(config, dict):
            raise CodexPluginManifestError(f"{label} entry {name!r} must be an object")
        normalized[name] = config
    return normalized


def _component_paths(root: Path, manifest: CodexPluginManifest) -> dict[str, tuple[Path, ...]]:
    paths: dict[str, list[Path]] = {"skills": [], "mcp": [], "hooks": [], "apps": []}

    def add(
        kind: str,
        declarations: Iterable[str],
        defaults: Iterable[str],
        *,
        expected: Literal["file", "directory", "either"],
    ) -> None:
        default_paths = tuple(defaults)
        raw_paths = (*default_paths, *declarations)
        seen: set[Path] = set()
        for raw_path in raw_paths:
            candidate = root / PurePosixPath(raw_path)
            if raw_path in default_paths and not candidate.exists():
                continue
            resolved = _contained_path(
                root,
                raw_path,
                label=f"{kind} component path",
                expected=expected,
            )
            if resolved not in seen:
                paths[kind].append(resolved)
                seen.add(resolved)

    skill_paths = (
        (manifest.skills,)
        if isinstance(manifest.skills, str)
        else tuple(manifest.skills or ())
    )
    add("skills", skill_paths, (), expected="either")
    mcp_path = manifest.mcp_servers if isinstance(manifest.mcp_servers, str) else None
    add("mcp", (mcp_path,) if mcp_path else (), (), expected="file")
    hook_paths: tuple[str, ...]
    if isinstance(manifest.hooks, str):
        hook_paths = (manifest.hooks,)
    elif isinstance(manifest.hooks, tuple) and all(
        isinstance(item, str) for item in manifest.hooks
    ):
        hook_paths = manifest.hooks
    else:
        hook_paths = ()
    hook_defaults = ("./hooks/hooks.json",) if manifest.hooks is None else ()
    add("hooks", hook_paths, hook_defaults, expected="file")
    add("apps", (manifest.apps,) if manifest.apps else (), (), expected="file")
    return {kind: tuple(value) for kind, value in paths.items()}


def _load_mcp_servers(
    root: Path,
    manifest: CodexPluginManifest,
    paths: tuple[Path, ...],
) -> list[tuple[str, dict[str, Any], str | None]]:
    entries: list[tuple[str, dict[str, Any], str | None]] = []
    if isinstance(manifest.mcp_servers, dict):
        for name, config in _validate_named_object_map(
            manifest.mcp_servers, label="plugin.json mcpServers"
        ).items():
            entries.append((name, config, None))
    for path in paths:
        payload, _raw = _read_json_object(path, root=root, label="Codex MCP manifest")
        if "mcpServers" in payload and "mcp_servers" in payload:
            raise CodexPluginManifestError(
                "Codex MCP manifest cannot contain both 'mcpServers' and 'mcp_servers'"
            )
        if "mcpServers" in payload:
            servers = payload["mcpServers"]
            unexpected = set(payload) - {"mcpServers"}
        elif "mcp_servers" in payload:
            servers = payload["mcp_servers"]
            unexpected = set(payload) - {"mcp_servers"}
        else:
            servers = payload
            unexpected = set()
        if unexpected:
            raise CodexPluginManifestError(
                "Codex MCP manifest wrapper cannot contain unrelated fields: "
                + ", ".join(sorted(unexpected))
            )
        relative = path.relative_to(root).as_posix()
        for name, config in _validate_named_object_map(
            servers, label="Codex MCP manifest servers"
        ).items():
            entries.append((name, config, relative))
    _reject_duplicate_names(entries, label="MCP server")
    return entries


def _load_named_companion(
    root: Path,
    paths: tuple[Path, ...],
    *,
    key: str,
    label: str,
) -> list[tuple[str, dict[str, Any], str]]:
    entries: list[tuple[str, dict[str, Any], str]] = []
    for path in paths:
        payload, _raw = _read_json_object(path, root=root, label=label)
        if key not in payload:
            raise CodexPluginManifestError(f"{label} must contain a {key!r} object")
        unexpected = set(payload) - {key}
        if unexpected:
            raise CodexPluginManifestError(
                f"{label} cannot contain unrelated fields: " + ", ".join(sorted(unexpected))
            )
        relative = path.relative_to(root).as_posix()
        named_entries = _validate_named_object_map(payload[key], label=f"{label} {key}")
        for name, config in named_entries.items():
            entries.append((name, config, relative))
    _reject_duplicate_names(entries, label=key.rstrip("s"))
    return entries


def _load_hooks(
    root: Path,
    manifest: CodexPluginManifest,
    paths: tuple[Path, ...],
) -> list[tuple[str, dict[str, Any], str | None]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, set[str | None]] = {}

    def append_payload(payload: dict[str, Any], *, relative: str | None) -> None:
        unexpected = set(payload) - {"description", "hooks"}
        if unexpected:
            raise CodexPluginManifestError(
                "Codex hooks manifest cannot contain unrelated fields: "
                + ", ".join(sorted(unexpected))
            )
        description = payload.get("description")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            raise CodexPluginManifestError(
                "Codex hooks manifest description must be a non-empty string"
            )
        if "hooks" not in payload or not isinstance(payload["hooks"], dict):
            raise CodexPluginManifestError(
                "Codex hooks manifest must contain a 'hooks' object"
            )
        for event, hooks in payload["hooks"].items():
            if not isinstance(event, str) or not event.strip():
                raise CodexPluginManifestError(
                    "Codex hook event names must be non-empty strings"
                )
            if not isinstance(hooks, list) or not all(
                isinstance(item, dict) for item in hooks
            ):
                raise CodexPluginManifestError(
                    f"Codex hooks for event {event!r} must be an array of objects"
                )
            grouped.setdefault(event, []).extend(hooks)
            sources.setdefault(event, set()).add(relative)

    inline: tuple[dict[str, Any], ...] = ()
    if isinstance(manifest.hooks, dict):
        inline = (manifest.hooks,)
    elif isinstance(manifest.hooks, tuple) and all(
        isinstance(item, dict) for item in manifest.hooks
    ):
        inline = manifest.hooks
    for payload in inline:
        append_payload(payload, relative=None)
    for path in paths:
        payload, _raw = _read_json_object(path, root=root, label="Codex hooks manifest")
        relative = path.relative_to(root).as_posix()
        append_payload(payload, relative=relative)
    return [
        (
            event,
            {"hooks": grouped[event]},
            next(iter(sources[event]))
            if len(sources[event]) == 1 and None not in sources[event]
            else None,
        )
        for event in sorted(grouped)
    ]


def _reject_duplicate_names(
    entries: Iterable[tuple[str, dict[str, Any], str | None]], *, label: str
) -> None:
    seen: set[str] = set()
    for name, _config, _path in entries:
        if name in seen:
            raise CodexPluginManifestError(f"duplicate {label} name: {name!r}")
        seen.add(name)


def _iter_skill_roots(root: Path, paths: tuple[Path, ...]) -> list[tuple[str, Path]]:
    skills: list[tuple[str, Path]] = []
    names: set[str] = set()
    for path in paths:
        candidates: list[Path]
        if path.is_file():
            if path.name != "SKILL.md":
                raise CodexPluginManifestError("a file-valued skills path must name SKILL.md")
            candidates = [path.parent]
        else:
            direct_manifest = path / "SKILL.md"
            if direct_manifest.is_file():
                candidates = [path]
            else:
                candidates = [
                    child
                    for child in sorted(path.iterdir(), key=lambda item: item.name)
                    if not child.name.startswith(".") and child.is_dir()
                ]
        for skill_root in candidates:
            relative = skill_root.relative_to(root).as_posix()
            _contained_path(
                root,
                relative,
                label="skill directory",
                expected="directory",
            )
            skill_manifest = skill_root / "SKILL.md"
            if not skill_manifest.is_file() or skill_manifest.is_symlink():
                raise CodexPluginManifestError(f"skill {relative!r} is missing a regular SKILL.md")
            name = skill_root.name
            if name in names:
                raise CodexPluginManifestError(f"duplicate skill directory name: {name!r}")
            names.add(name)
            skills.append((name, skill_root))
    return skills


def _validate_interface_assets(root: Path, interface: CodexPluginInterface | None) -> None:
    if interface is None:
        return
    references = [
        interface.composer_icon,
        interface.logo,
        interface.logo_dark,
        *interface.screenshots,
    ]
    for reference in references:
        if reference is not None:
            _contained_path(root, reference, label="interface asset", expected="file")


def _load_installed_plugin(
    plugin_root: Path,
) -> tuple[
    Path,
    CodexPluginManifest,
    bytes,
    list[tuple[str, Path]],
    list[tuple[str, dict[str, Any], str | None]],
    list[tuple[str, dict[str, Any], str | None]],
    list[tuple[str, dict[str, Any], str]],
]:
    root = _validate_root(plugin_root)
    manifest_path = root / _MANIFEST_RELATIVE_PATH
    payload, raw = _read_json_object(
        manifest_path,
        root=root,
        label=".codex-plugin/plugin.json",
    )
    try:
        manifest = CodexPluginManifest.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise CodexPluginManifestError(f"invalid .codex-plugin/plugin.json: {exc}") from exc

    paths = _component_paths(root, manifest)
    skills = _iter_skill_roots(root, paths["skills"])
    mcp_servers = _load_mcp_servers(root, manifest, paths["mcp"])
    hooks = _load_hooks(root, manifest, paths["hooks"])
    apps = _load_named_companion(
        root,
        paths["apps"],
        key="apps",
        label="Codex app manifest",
    )
    _validate_interface_assets(root, manifest.interface)
    return root, manifest, raw, skills, mcp_servers, hooks, apps


def load_codex_plugin_manifest(plugin_root: Path) -> CodexPluginManifest:
    """Load one installed Codex manifest and validate all referenced components."""

    _root, manifest, _raw, _skills, _mcp, _hooks, _apps = _load_installed_plugin(plugin_root)
    return manifest


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path]
    if path.is_file():
        files = [path]
        base = path.parent
    else:
        files = []
        base = path
        for candidate in sorted(
            path.rglob("*"),
            key=lambda item: item.relative_to(path).as_posix(),
        ):
            relative = candidate.relative_to(path).as_posix()
            if candidate.is_symlink():
                raise CodexPluginManifestError(
                    f"plugin snapshots must not include symlinks: {relative}"
                )
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise CodexPluginManifestError(
                    f"plugin snapshots may contain regular files only: {relative}"
                )
            files.append(candidate)
    if len(files) > _MAX_ARTIFACT_FILES:
        raise CodexPluginManifestError(
            f"plugin snapshot exceeds {_MAX_ARTIFACT_FILES} regular files"
        )
    total_bytes = 0
    for candidate in files:
        relative = candidate.relative_to(base).as_posix()
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise CodexPluginManifestError(f"plugin file cannot be read: {relative}") from exc
        total_bytes += len(raw)
        if total_bytes > _MAX_ARTIFACT_BYTES:
            raise CodexPluginManifestError(
                f"plugin snapshot exceeds {_MAX_ARTIFACT_BYTES} content bytes"
            )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(candidate.stat().st_mode) & 0o111:03o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def snapshot_installed_codex_plugin(
    plugin_root: Path,
    *,
    source: CodexPluginSourceCoordinate,
) -> CodexInstalledPluginSnapshot:
    """Create a deterministic installed-root and per-component snapshot."""

    root, manifest, raw, skills, mcp_servers, hooks, apps = _load_installed_plugin(plugin_root)
    components: list[CodexPluginComponentSnapshot] = []
    for name, skill_root in skills:
        components.append(
            CodexPluginComponentSnapshot(
                kind="skill",
                name=name,
                path=skill_root.relative_to(root).as_posix(),
                content_digest=_tree_digest(skill_root),
            )
        )
    for name, config, path in mcp_servers:
        components.append(
            CodexPluginComponentSnapshot(
                kind="mcp",
                name=name,
                path=path,
                content_digest=_canonical_json_digest({"name": name, "config": config}),
            )
        )
    for name, config, path in hooks:
        components.append(
            CodexPluginComponentSnapshot(
                kind="hook",
                name=name,
                path=path,
                content_digest=_canonical_json_digest({"name": name, "config": config}),
            )
        )
    for name, config, path in apps:
        components.append(
            CodexPluginComponentSnapshot(
                kind="app",
                name=name,
                path=path,
                content_digest=_canonical_json_digest({"name": name, "config": config}),
            )
        )
    return CodexInstalledPluginSnapshot(
        installed_root=str(root),
        source=source,
        manifest=manifest,
        manifest_digest=_sha256(raw),
        artifact_digest=_tree_digest(root),
        components=tuple(components),
    )


__all__ = [
    "CodexInstalledPluginSnapshot",
    "CodexPluginAuthor",
    "CodexPluginComponentSnapshot",
    "CodexPluginInterface",
    "CodexPluginManifest",
    "CodexPluginManifestError",
    "CodexPluginSourceCoordinate",
    "load_codex_plugin_manifest",
    "snapshot_installed_codex_plugin",
]
