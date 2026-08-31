"""Resolve the plugin composition embedded in an AgentBundle v2 directory.

``BundleManifest`` remains the single wire contract owned by Studio.  This
module only creates a runtime view after the existing manifest, file digests,
composition profile, and plugin lock have been checked against one another and
against the selected :class:`PluginRegistry`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import ValidationError

from ksadk.plugins.bundle_security import BundleSecurityError, assert_bundle_security
from ksadk.plugins.contracts import CompositionProfile, PluginLock
from ksadk.plugins.resolver import PluginRegistry, PluginResolutionError, ResolvedComposition
from ksadk.studio.contracts import BundleManifest


class PluginBundleError(ValueError):
    """Stable rejection while loading the plugin portion of a Bundle v2."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedPluginBundle:
    """Validated runtime view of existing AgentBundle v2 artifacts.

    This deliberately is not another serializable bundle contract.  Providers
    receive the already-resolved composition and the immutable resolved Agent
    spec; they do not re-read mutable Studio state.
    """

    root: Path
    manifest: BundleManifest
    resolved_agent_spec: Mapping[str, Any]
    composition: ResolvedComposition

    @property
    def bundle_digest(self) -> str:
        return str(self.manifest.bundle_digest)


class PluginBundleResolver:
    """Load and cross-check the composition artifacts of an AgentBundle v2."""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def resolve(self, root: str | Path) -> ResolvedPluginBundle:
        bundle_root = Path(root).resolve()
        if not bundle_root.is_dir():
            raise PluginBundleError(
                "plugin_bundle_unavailable",
                f"AgentBundle directory does not exist: {bundle_root}",
            )

        manifest_payload = self._read_json(bundle_root, "manifest.json")
        try:
            manifest = BundleManifest.model_validate(manifest_payload)
        except ValidationError as error:
            raise PluginBundleError("plugin_bundle_manifest_invalid", str(error)) from error
        if manifest.bundle_format != "agentkit.bundle/v2":
            raise PluginBundleError(
                "plugin_bundle_version_unsupported",
                "PluginHost execution requires agentkit.bundle/v2",
            )
        if manifest.execution_profile != "composed":
            raise PluginBundleError(
                "plugin_bundle_legacy_execution",
                "Bundle v2 selects legacy execution and cannot enter PluginHost",
            )

        self._verify_manifest_digest(manifest)
        self._verify_declared_files(bundle_root, manifest)
        try:
            assert_bundle_security(bundle_root)
        except BundleSecurityError as error:
            raise PluginBundleError(
                error.code,
                "Plugin Bundle contains literal secret material",
            ) from error

        profile_payload = self._read_json(bundle_root, "composition-profile.json")
        lock_payload = self._read_json(bundle_root, "plugin-lock.json")
        agent_spec = self._read_json(bundle_root, "resolved-agent-spec.json")
        try:
            profile = CompositionProfile.model_validate(profile_payload)
            embedded_lock = PluginLock.model_validate(lock_payload)
        except ValidationError as error:
            raise PluginBundleError("plugin_bundle_composition_invalid", str(error)) from error

        try:
            composition = self._registry.resolve(profile)
        except PluginResolutionError as error:
            raise PluginBundleError(error.code, str(error)) from error
        if composition.profile_digest != manifest.composition_profile_digest:
            raise PluginBundleError(
                "plugin_bundle_profile_digest_mismatch",
                "composition-profile.json does not match manifest compositionProfileDigest",
            )
        if embedded_lock != composition.plugin_lock:
            raise PluginBundleError(
                "plugin_bundle_lock_mismatch",
                "plugin-lock.json does not match deterministic profile resolution",
            )
        if composition.plugin_lock_digest != manifest.plugin_lock_digest:
            raise PluginBundleError(
                "plugin_bundle_lock_digest_mismatch",
                "plugin-lock.json does not match manifest pluginLockDigest",
            )

        return ResolvedPluginBundle(
            root=bundle_root,
            manifest=manifest,
            resolved_agent_spec=_deep_freeze(agent_spec),
            composition=composition,
        )

    @staticmethod
    def _read_json(root: Path, relative: str) -> dict[str, Any]:
        path = root / relative
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise PluginBundleError(
                "plugin_bundle_file_missing", f"Bundle file is missing: {relative}"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PluginBundleError(
                "plugin_bundle_file_invalid", f"Bundle file is invalid: {relative}"
            ) from error
        if not isinstance(payload, dict):
            raise PluginBundleError(
                "plugin_bundle_file_invalid", f"Bundle file must be an object: {relative}"
            )
        return payload

    @staticmethod
    def _verify_manifest_digest(manifest: BundleManifest) -> None:
        payload = manifest.model_dump(
            by_alias=True,
            exclude={"bundle_digest"},
            exclude_none=True,
            mode="json",
        )
        actual = _sha256(_canonical_json(payload))
        if manifest.bundle_digest != actual:
            raise PluginBundleError(
                "plugin_bundle_digest_mismatch",
                "manifest.json does not match its declared bundleDigest",
            )

    @staticmethod
    def _verify_declared_files(root: Path, manifest: BundleManifest) -> None:
        declared: set[str] = set()
        for entry in manifest.files:
            relative = PurePosixPath(entry.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise PluginBundleError(
                    "plugin_bundle_path_invalid",
                    f"Bundle manifest contains an unsafe path: {entry.path}",
                )
            path = (root / Path(*relative.parts)).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise PluginBundleError(
                    "plugin_bundle_path_invalid",
                    f"Bundle manifest path escapes its root: {entry.path}",
                ) from error
            try:
                content = path.read_bytes()
            except OSError as error:
                raise PluginBundleError(
                    "plugin_bundle_file_missing",
                    f"Declared Bundle file is missing: {entry.path}",
                ) from error
            if len(content) != entry.size or _sha256(content) != entry.sha256:
                raise PluginBundleError(
                    "plugin_bundle_file_digest_mismatch",
                    f"Declared Bundle file failed integrity check: {entry.path}",
                )
            declared.add(entry.path)

        required = {
            "composition-profile.json",
            "plugin-lock.json",
            "resolved-agent-spec.json",
        }
        missing = sorted(required - declared)
        if missing:
            raise PluginBundleError(
                "plugin_bundle_file_missing",
                "Bundle manifest does not declare required plugin artifacts: " + ", ".join(missing),
            )
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        undeclared = sorted(actual - declared)
        if undeclared:
            raise PluginBundleError(
                "plugin_bundle_file_undeclared",
                "Bundle contains files outside its integrity manifest: " + ", ".join(undeclared),
            )


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _deep_freeze(value: Any) -> Any:
    """Prevent a Provider from mutating any nested resolved-spec value."""

    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


__all__ = [
    "PluginBundleError",
    "PluginBundleResolver",
    "ResolvedPluginBundle",
]
