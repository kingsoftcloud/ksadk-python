"""Central compatibility boundary for pre-DSH Harness AgentBundles.

Reading an old Bundle and selecting the in-process Harness provider are two
different decisions.  Existing ADK, LangGraph and Codex artifacts continue to
use their established adapters.  The Harness compatibility adapter is only
available for an immutable Bundle v1 digest that a release/migration owner has
explicitly registered as a historical Harness artifact.

Bundle v2 is never eligible for this bridge.  It must resolve the Harness
provider from the active DSH registration and its deterministic PluginLock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Literal

from pydantic import ValidationError

from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.providers.legacy_catalog import (
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    legacy_harness_agent_provider_manifest,
)
from ksadk.studio.capabilities import compute_bundle_digest
from ksadk.studio.contracts import BundleManifest


class LegacyBundleCompatibilityError(ValueError):
    """Stable fail-closed result from the legacy Bundle normalizer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LegacyHarnessSource:
    """Immutable identity approved by a release or migration process."""

    bundle_digest: str
    resolved_digest: str
    source_revision: int

    @classmethod
    def from_verified_manifest(cls, manifest: BundleManifest) -> "LegacyHarnessSource":
        """Capture an already-verified historical Bundle v1 identity."""

        if manifest.bundle_format != "agentkit.bundle/v1":
            raise LegacyBundleCompatibilityError(
                "legacy_harness_bundle_version_unsupported",
                "Only an AgentBundle v1 can be registered as a legacy Harness source",
            )
        _require_self_consistent_digest(manifest)
        return cls(
            bundle_digest=manifest.bundle_digest,
            resolved_digest=manifest.resolved_digest,
            source_revision=manifest.source_revision,
        )


@dataclass(frozen=True)
class HarnessProviderSelection:
    """Normalized provider choice; execution stays with the owning runtime."""

    route: Literal["dsh", "legacy"]
    manifest: PluginManifest | None = None


class LegacyBundleAdapter:
    """Select Harness compatibility without broad version-based fallback."""

    def __init__(self, sources: Iterable[LegacyHarnessSource] = ()) -> None:
        source_by_digest: dict[str, LegacyHarnessSource] = {}
        for source in sources:
            previous = source_by_digest.setdefault(source.bundle_digest, source)
            if previous != source:
                raise ValueError("conflicting legacy Harness identities use the same bundle digest")
        self._sources = source_by_digest

    def select_from_bundle(
        self,
        root: str | Path,
        *,
        registered_provider_ids: Iterable[str] = (),
    ) -> tuple[BundleManifest, HarnessProviderSelection | None]:
        """Verify one immutable Bundle before applying the compatibility rule."""

        bundle_root = Path(root).resolve()
        try:
            payload = json.loads((bundle_root / "manifest.json").read_text(encoding="utf-8"))
            manifest = BundleManifest.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise LegacyBundleCompatibilityError(
                "bundle_manifest_invalid", "AgentBundle manifest is invalid"
            ) from error
        _require_self_consistent_digest(manifest)
        declared: set[str] = set()
        for entry in manifest.files:
            relative = PurePosixPath(entry.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise LegacyBundleCompatibilityError(
                    "bundle_path_invalid", "AgentBundle contains an unsafe file path"
                )
            path = (bundle_root / Path(*relative.parts)).resolve()
            if not path.is_relative_to(bundle_root):
                raise LegacyBundleCompatibilityError(
                    "bundle_path_invalid", "AgentBundle file path escapes its root"
                )
            try:
                content = path.read_bytes()
            except OSError as error:
                raise LegacyBundleCompatibilityError(
                    "bundle_file_missing", f"AgentBundle file is missing: {entry.path}"
                ) from error
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if len(content) != entry.size or digest != entry.sha256:
                raise LegacyBundleCompatibilityError(
                    "bundle_file_digest_mismatch",
                    f"AgentBundle file failed integrity verification: {entry.path}",
                )
            declared.add(entry.path)
        actual = {
            path.relative_to(bundle_root).as_posix()
            for path in bundle_root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        if actual != declared:
            raise LegacyBundleCompatibilityError(
                "bundle_membership_mismatch",
                "AgentBundle contains missing or undeclared files",
            )
        return manifest, self.select_harness_provider(
            manifest,
            registered_provider_ids=registered_provider_ids,
        )

    def select_harness_provider(
        self,
        manifest: BundleManifest,
        *,
        registered_provider_ids: Iterable[str] = (),
    ) -> HarnessProviderSelection | None:
        """Return the exact Harness route, or ``None`` for non-Harness Bundles.

        For Bundle v1, a matching approved digest is the Harness discriminator;
        this also supports historical manifests that predate ``runtimeType``.
        For Bundle v2, ``runtimeType=harness`` always requires a ready DSH
        provider registration.  No v1 allowlist entry can change that rule.
        """

        runtime_type = manifest.runtime_type.strip().lower()
        registered = frozenset(registered_provider_ids)

        if manifest.bundle_format == "agentkit.bundle/v2":
            if runtime_type != "harness":
                return None
            if KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID not in registered:
                raise LegacyBundleCompatibilityError(
                    "agent_provider_not_registered",
                    "Harness Bundle v2 requires a ready DSH provider registration",
                )
            return HarnessProviderSelection(route="dsh")

        source = self._sources.get(manifest.bundle_digest)
        if source is None:
            if runtime_type == "harness":
                raise LegacyBundleCompatibilityError(
                    "legacy_harness_source_unrecognized",
                    "Bundle v1 is not an explicitly registered historical Harness artifact",
                )
            return None

        _require_self_consistent_digest(manifest)
        if (
            manifest.resolved_digest != source.resolved_digest
            or manifest.source_revision != source.source_revision
        ):
            raise LegacyBundleCompatibilityError(
                "legacy_harness_source_mismatch",
                "Bundle v1 metadata does not match its registered historical identity",
            )
        if runtime_type not in {"", "harness"}:
            raise LegacyBundleCompatibilityError(
                "legacy_harness_runtime_mismatch",
                "Registered historical Harness Bundle declares another runtime type",
            )
        if manifest.plugin_lock_digest or manifest.composition_profile_digest:
            raise LegacyBundleCompatibilityError(
                "legacy_harness_composition_forbidden",
                "A legacy Harness Bundle cannot carry partial Bundle v2 composition state",
            )
        return HarnessProviderSelection(
            route="legacy",
            manifest=legacy_harness_agent_provider_manifest(),
        )


def _require_self_consistent_digest(manifest: BundleManifest) -> None:
    if not manifest.bundle_digest or manifest.bundle_digest != compute_bundle_digest(manifest):
        raise LegacyBundleCompatibilityError(
            "legacy_harness_bundle_digest_mismatch",
            "Historical Harness manifest does not match its declared bundle digest",
        )


__all__ = [
    "HarnessProviderSelection",
    "LegacyBundleAdapter",
    "LegacyBundleCompatibilityError",
    "LegacyHarnessSource",
]
