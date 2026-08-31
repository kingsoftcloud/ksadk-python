"""Pure, deterministic Profile -> PluginLock resolver.

This module is intentionally side-effect free: it resolves only supplied
manifests and never imports their entrypoints.  PluginHost will later own
admission, staging, start, health, and disposal.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from ksadk.plugins.contracts import (
    CompositionProfile,
    LockedCapability,
    PluginDependency,
    PluginLock,
    PluginLockEntry,
    PluginManifest,
    PluginReference,
    plugin_lock_digest,
)


class PluginResolutionError(ValueError):
    """Stable, typed rejection from the pure composition resolver."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedComposition:
    profile: CompositionProfile
    profile_digest: str
    plugin_lock: PluginLock
    plugin_lock_digest: str
    # Build-time audit facts from the exact manifests that produced the lock.
    # This is an internal immutable projection, not another wire manifest: the
    # public Bundle continues to bind only CompositionProfile and PluginLock.
    manifests: tuple[PluginManifest, ...] = ()


def canonical_composition_profile(profile: CompositionProfile) -> bytes:
    return json.dumps(
        profile.model_dump(by_alias=True, exclude_none=True, mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def composition_profile_digest(profile: CompositionProfile) -> str:
    return f"sha256:{hashlib.sha256(canonical_composition_profile(profile)).hexdigest()}"


class PluginRegistry:
    """Immutable in-memory manifest catalog used by build-time resolution."""

    def __init__(self, manifests: Iterable[PluginManifest]) -> None:
        indexed: dict[tuple[str, str], PluginManifest] = {}
        for manifest in manifests:
            key = (manifest.metadata.id, manifest.metadata.version)
            if key in indexed:
                raise PluginResolutionError(
                    "plugin_manifest_duplicate",
                    f"duplicate plugin manifest {manifest.metadata.id}@{manifest.metadata.version}",
                )
            indexed[key] = manifest
        self._manifests = indexed

    def resolve(self, profile: CompositionProfile) -> ResolvedComposition:
        """Resolve the requested graph without running any plugin code."""

        selected: dict[str, PluginManifest] = {}
        dependencies: dict[str, set[str]] = {}

        def select_reference(reference: PluginReference) -> PluginManifest:
            plugin_id, version = _parse_plugin_reference(reference.ref)
            manifest = self._manifests.get((plugin_id, version))
            if manifest is None:
                raise PluginResolutionError(
                    "plugin_manifest_unresolved",
                    f"plugin {plugin_id}@{version} is not in the selected catalog",
                )
            select_manifest(manifest)
            return manifest

        def select_manifest(manifest: PluginManifest) -> None:
            plugin_id = manifest.metadata.id
            if plugin_id in selected:
                if selected[plugin_id].metadata.version != manifest.metadata.version:
                    raise PluginResolutionError(
                        "plugin_version_conflict",
                        f"profile requires conflicting versions of plugin {plugin_id}",
                    )
                return
            selected[plugin_id] = manifest
            dependencies.setdefault(plugin_id, set())
            for requirement in manifest.spec.requires:
                provider = self._resolve_requirement(requirement.definition, requirement.version)
                if provider.metadata.id == plugin_id:
                    raise PluginResolutionError(
                        "plugin_dependency_cycle",
                        f"plugin {plugin_id} cannot provide its own required capability",
                    )
                dependencies[plugin_id].add(provider.metadata.id)
                select_manifest(provider)

        provider = select_reference(profile.agent_provider)
        self._require_agent_provider(provider)
        for capability in profile.capabilities:
            if capability.ref.startswith("plugin://"):
                # A capability entry uses the same pinning grammar as the
                # agent provider, but it has no special agent-provider role.
                plugin_id, version = _parse_plugin_reference(capability.ref)
                manifest = self._manifests.get((plugin_id, version))
                if manifest is None:
                    raise PluginResolutionError(
                        "plugin_manifest_unresolved",
                        f"plugin {plugin_id}@{version} is not in the selected catalog",
                    )
                select_manifest(manifest)

        self._validate_unique_slots(selected.values())
        lock = self._build_lock(selected, dependencies)
        return ResolvedComposition(
            profile=profile,
            profile_digest=composition_profile_digest(profile),
            plugin_lock=lock,
            plugin_lock_digest=plugin_lock_digest(lock),
            manifests=tuple(
                sorted(
                    selected.values(),
                    key=lambda item: (item.metadata.id, item.metadata.version),
                )
            ),
        )

    def manifest_for(self, plugin_id: str, version: str) -> PluginManifest:
        """Return an exact manifest already admitted to this local catalog."""

        manifest = self._manifests.get((plugin_id, version))
        if manifest is None:
            raise PluginResolutionError(
                "plugin_manifest_unresolved",
                f"plugin {plugin_id}@{version} is not in the selected catalog",
            )
        return manifest

    def _resolve_requirement(self, definition: str, constraint: str) -> PluginManifest:
        candidates = [
            manifest
            for manifest in self._manifests.values()
            if any(offer.definition == definition for offer in manifest.spec.provides)
            and _version_satisfies(manifest.metadata.version, constraint)
        ]
        if not candidates:
            raise PluginResolutionError(
                "plugin_requirement_unresolved",
                f"no plugin provides {definition!r} matching {constraint!r}",
            )
        if len(candidates) > 1:
            names = ", ".join(
                f"{item.metadata.id}@{item.metadata.version}"
                for item in sorted(
                    candidates,
                    key=lambda item: (item.metadata.id, item.metadata.version),
                )
            )
            raise PluginResolutionError(
                "plugin_requirement_ambiguous",
                f"multiple plugins provide {definition!r}: {names}",
            )
        return candidates[0]

    @staticmethod
    def _require_agent_provider(manifest: PluginManifest) -> None:
        if any(
            offer.definition == "agent.provider/v1"
            and offer.slot == "agent.execution"
            and offer.mode == "unique"
            for offer in manifest.spec.provides
        ):
            return
        raise PluginResolutionError(
            "agent_provider_invalid",
            f"plugin {manifest.metadata.id}@{manifest.metadata.version} "
            "does not own agent.execution",
        )

    @staticmethod
    def _validate_unique_slots(manifests: Iterable[PluginManifest]) -> None:
        owners: dict[str, str] = {}
        for manifest in manifests:
            for offer in manifest.spec.provides:
                if offer.mode != "unique":
                    continue
                existing = owners.get(offer.slot)
                if existing and existing != manifest.metadata.id:
                    raise PluginResolutionError(
                        "plugin_slot_conflict",
                        f"unique slot {offer.slot!r} is owned by both {existing} "
                        f"and {manifest.metadata.id}",
                    )
                owners[offer.slot] = manifest.metadata.id

    @staticmethod
    def _build_lock(
        selected: dict[str, PluginManifest], dependencies: dict[str, set[str]]
    ) -> PluginLock:
        entries: list[PluginLockEntry] = []
        for plugin_id, manifest in selected.items():
            entries.append(
                PluginLockEntry(
                    id=plugin_id,
                    version=manifest.metadata.version,
                    digest=manifest.spec.provenance.digest,
                    source=manifest.spec.provenance.source,
                    signature_ref=manifest.spec.provenance.signature_ref,
                    license=manifest.spec.provenance.license,
                    provides=[
                        LockedCapability(
                            definition=offer.definition,
                            slot=offer.slot,
                            owner=plugin_id,
                        )
                        for offer in manifest.spec.provides
                    ],
                    dependencies=[
                        PluginDependency(
                            id=dependency_id,
                            version=selected[dependency_id].metadata.version,
                            digest=selected[dependency_id].spec.provenance.digest,
                        )
                        for dependency_id in sorted(dependencies.get(plugin_id, set()))
                    ],
                )
            )
        return PluginLock(plugins=entries)


def _parse_plugin_reference(value: str) -> tuple[str, str]:
    # The Pydantic type validates this at the public boundary.  Keeping the
    # parser defensive makes this pure resolver safe for values reconstructed
    # by other language consumers.
    if not value.startswith("plugin://") or "@" not in value:
        raise PluginResolutionError(
            "plugin_reference_invalid", f"invalid plugin reference {value!r}"
        )
    plugin_id, version = value.removeprefix("plugin://").rsplit("@", 1)
    return plugin_id, version


_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?(\d+)\.(\d+)\.(\d+)$")


def _version_satisfies(version: str, constraint: str) -> bool:
    """Small exact-range evaluator for manifest capability requirements.

    The manifest accepts a SemVer range string rather than an unbounded
    dependency resolver.  P2-00A supports the explicit comparator form used
    by the contract examples (for example ``>=1,<2``); unsupported forms are
    rejected instead of silently accepting a possibly incompatible plugin.
    """

    parsed_version = _parse_release(version, field="plugin version")
    comparators = [part.strip() for part in constraint.split(",") if part.strip()]
    if not comparators:
        raise PluginResolutionError("plugin_constraint_invalid", "empty plugin requirement version")
    for comparator in comparators:
        match = _COMPARATOR.fullmatch(comparator)
        if match is None:
            # ``>=1,<2`` is a concise allowed major range in the published
            # manifest.  Expand it deterministically rather than treating it
            # as a lax string comparison.
            major_range = re.fullmatch(r"(>=|>|<=|<)(\d+)", comparator)
            if major_range is None:
                raise PluginResolutionError(
                    "plugin_constraint_invalid",
                    f"unsupported plugin requirement version {constraint!r}",
                )
            operator, major = major_range.groups()
            target = (int(major), 0, 0)
        else:
            operator = match.group(1) or "="
            target = (
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
            )
        if not _compare(parsed_version, target, operator):
            return False
    return True


def version_satisfies(version: str, constraint: str) -> bool:
    """Evaluate the exact compatibility-range grammar used by plugin manifests."""

    return _version_satisfies(version, constraint)


def _parse_release(value: str, *, field: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise PluginResolutionError("plugin_constraint_invalid", f"invalid {field} {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _compare(left: tuple[int, int, int], right: tuple[int, int, int], operator: str) -> bool:
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
        "=": left == right,
    }[operator]


__all__ = [
    "PluginRegistry",
    "PluginResolutionError",
    "ResolvedComposition",
    "canonical_composition_profile",
    "composition_profile_digest",
    "version_satisfies",
]
