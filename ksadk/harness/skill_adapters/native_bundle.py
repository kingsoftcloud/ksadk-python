"""Project PluginHost-locked Bundle Skills into Native's disclosure source.

The PluginHost owns discovery and the Bundle digest. Native only receives the
already admitted resources, rechecking their content when a disclosure is read
so an activation cannot silently switch to changed files.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path, PurePosixPath

from ksadk.harness.skill_runtime import LocalSkillSource, SkillDisclosureError, SkillManifest
from ksadk.plugins.builtins import _directory_digest, _directory_resource_digests
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.harness import HarnessEngineActivationContext, HarnessSkillContribution


class NativeBundleSkillSource:
    """Read only the exact, individually pinned Skills from one Bundle."""

    def __init__(
        self, *, bundle_root: Path, contributions: tuple[HarnessSkillContribution, ...]
    ) -> None:
        root = bundle_root.resolve(strict=True)
        resources = {}
        entries: dict[str, tuple[Path, str]] = {}
        for item in contributions:
            ref, digest, path = item.source_ref, item.content_digest, item.resource_root
            if (
                not ref
                or not ref.startswith("skill:")
                or not digest
                or path is None
                or ref in entries
            ):
                raise ValueError("native_bundle_skill_binding_invalid")
            try:
                relative = path.relative_to(root)
                if not relative.parts:
                    raise ValueError("native_bundle_skill_path_invalid")
                for index in range(1, len(relative.parts) + 1):
                    cursor = root.joinpath(*relative.parts[:index])
                    if cursor.is_symlink():
                        raise ValueError("native_bundle_skill_path_invalid")
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as error:
                raise ValueError("native_bundle_skill_path_invalid") from error
            if not resolved.is_dir():
                raise ValueError("native_bundle_skill_path_invalid")
            actual, resource_digests = _directory_resource_digests(resolved)
            if actual != digest:
                raise ValueError("native_bundle_skill_digest_mismatch")
            entries[ref] = (resolved, digest)
            resources[ref] = resource_digests
        self._entries = entries
        self._resources = resources
        self._delegate = LocalSkillSource({ref: path for ref, (path, _digest) in entries.items()})

    @property
    def skill_refs(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def _check(self, skill_id: str) -> None:
        try:
            path, expected = self._entries[skill_id]
        except KeyError:
            raise KeyError(f"Skill {skill_id!r} is not bound") from None
        try:
            if path.resolve(strict=True) != path or any(
                item.is_symlink() for item in path.rglob("*")
            ):
                raise SkillDisclosureError("bound Skill path changed after activation")
            actual = _directory_digest(path)
        except (OSError, ValueError, PluginHostError) as error:
            raise SkillDisclosureError("bound Skill content is unavailable") from error
        if actual != expected:
            raise SkillDisclosureError("bound Skill content changed after activation")

    def assert_available(self, skill_id: str) -> None:
        """Check a locked resource before a host exposes its admitted snapshot."""
        self._check(skill_id)

    def bound_resource_refs(self, skill_id: str) -> tuple[str, ...]:
        """Expected package metadata, not a grant or live availability proof."""
        return tuple(self._resources[skill_id])

    def manifest(self, skill_id: str) -> SkillManifest:
        self._check(skill_id)
        result = replace(self._delegate.manifest(skill_id),
                         resource_refs=self.bound_resource_refs(skill_id))
        self._check(skill_id)
        return result

    def full_text(self, skill_id: str) -> str:
        self._check(skill_id)
        result = self._delegate.full_text(skill_id)
        self._check(skill_id)
        return result

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        self._check(skill_id)
        root, _digest = self._entries[skill_id]
        relative = PurePosixPath(resource_ref)
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in resource_ref
        ):
            raise SkillDisclosureError("bound Skill resource path is invalid")
        candidate = root.joinpath(*relative.parts)
        if any(
            root.joinpath(*relative.parts[:index]).is_symlink()
            for index in range(1, len(relative.parts) + 1)
        ):
            raise SkillDisclosureError("bound Skill resource path changed after activation")
        try:
            if not candidate.is_file() or candidate.stat().st_size > 32_768:
                raise SkillDisclosureError("bound Skill resource is unavailable or too large")
        except OSError as error:
            raise SkillDisclosureError("bound Skill resource is unavailable") from error
        result = self._delegate.resource(skill_id, resource_ref)
        if len(result) > 32_768:
            raise SkillDisclosureError("bound Skill resource is too large")
        expected = self._resources[skill_id].get(relative.as_posix())
        if expected != f"sha256:{hashlib.sha256(result).hexdigest()}":
            raise SkillDisclosureError("bound Skill resource bytes changed after activation")
        self._check(skill_id)
        return result


def native_bundle_skill_source(
    context: HarnessEngineActivationContext,
) -> NativeBundleSkillSource | None:
    """Return a source for the activation's Skills, never discovering extras."""

    if not context.skills:
        return None
    return NativeBundleSkillSource(
        bundle_root=context.bundle.root,
        contributions=context.skills,
    )


__all__ = ["NativeBundleSkillSource", "native_bundle_skill_source"]
