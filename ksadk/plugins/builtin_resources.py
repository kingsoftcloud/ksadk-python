"""Validated Bundle resource and configuration helpers for built-in capabilities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import plugin_lock_digest
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.resolver import composition_profile_digest


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
        item.id == plugin_id and item.version == version for item in composition.plugin_lock.plugins
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


def _resolved_resources(bundle: ResolvedPluginBundle, field: str) -> tuple[Mapping[str, Any], ...]:
    capabilities = _mapping(
        bundle.resolved_agent_spec.get("capabilities"),
        code="builtin_bundle_capabilities_missing",
    )
    raw = capabilities.get(field)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PluginHostError(
            "builtin_bundle_capabilities_missing", f"Bundle capabilities.{field} is missing"
        )
    return tuple(_mapping(item, code="builtin_bundle_capabilities_invalid") for item in raw)


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
    return _directory_resource_digests(directory, include_resources=False)[0]


def _directory_resource_digests(
    directory: Path, *, include_resources: bool = True,
) -> tuple[str, dict[str, str]]:
    """Capture package and per-file identities from the same byte reads."""
    digest = hashlib.sha256()
    resources = {}
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise PluginHostError(
                "builtin_skill_path_invalid", "Skill Bundle cannot contain symlinks"
            )
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        if include_resources:
            resources[relative.decode("utf-8")] = f"sha256:{hashlib.sha256(content).hexdigest()}"
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}", resources


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
