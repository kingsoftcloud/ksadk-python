"""Dynamic discovery with host-restored run selections in one admitted Skill space."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock

from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.resource_runtime.discovery_receipts import DiscoverySkillReceipt
from ksadk.resource_runtime.skills import (
    PinnedSkillService,
    SkillExecutionQuery,
    SkillResourceQuery,
    SkillSearchQuery,
)
from ksadk.resource_runtime.snapshots import SkillExecutionTarget
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime.registry import match_skill_refs
from ksadk.skills.service_client import SkillServiceClient


class DiscoverySkillService(PinnedSkillService):
    """Reuse the verified-package read contract while selecting from a live catalog.

    A first load fixes a version; every subsequent read checks cached bytes. The
    host commits a receipt before exposing content and supplies restored selections
    when resuming a logical run. This does not claim native engine discovery.
    """

    def __init__(
        self,
        config: ResourceConfig,
        client: SkillServiceClient,
        *,
        cache_directory: Path,
        namespace: str,
        restored_selections: tuple[DiscoverySkillReceipt, ...] = (),
        execution_target: SkillExecutionTarget | None = None,
    ):
        self.config = ResourceConfig.model_validate(config.model_dump())
        if (
            self.config.binding.resource.kind != "skill-space"
            or self.config.selection_mode != "discovery"
            or self.config.include_public
            or client.allow_env_fallback
        ):
            raise ValueError("Discovery requires one explicit non-public Skill space")
        self.client = client
        self.execution_target = (
            SkillExecutionTarget.model_validate(execution_target.model_dump())
            if execution_target is not None else None
        )
        if self.execution_target is not None and self.config.execution_mode != "isolated":
            raise ValueError("Discovery execution target requires isolated mode")
        self.store = PackageStore(cache_directory, namespace=namespace, require_hash=True)
        receipts = tuple(
            DiscoverySkillReceipt.model_validate(item.model_dump()) for item in restored_selections
        )
        if len(receipts) > 32 or len({item.skill_id for item in receipts}) != len(receipts):
            raise ValueError("Invalid restored discovery selections")
        self._selected = {item.skill_id: item.to_ref() for item in receipts}
        self._lock = RLock()

    def _catalog(self):
        with self._lock:
            listing = self.client.list_skills_by_space_id(self.config.binding.resource.id)
            if listing.space_id != self.config.binding.resource.id:
                raise SkillPackageError("Skill catalog does not match admitted space")
            refs = {}
            for ref in listing.active_skills(allow_partial=True):
                DiscoverySkillReceipt.from_ref(ref)
                if not ref.name or len(ref.name) > 256 or ref.skill_id in refs:
                    raise SkillPackageError("Skill catalog contains invalid identities")
                refs[ref.skill_id] = self._selected.get(ref.skill_id, ref)
            return refs, listing.truncated

    def list_skills(self, arguments: dict) -> dict:
        if arguments:
            raise ValueError("Skill catalog accepts no space override")
        refs, partial = self._catalog()
        ordered = sorted(refs.values(), key=lambda item: item.skill_id)
        return {
            "status": "ok",
            "truncated": partial or len(ordered) > 128,
            "items": [
                {
                    "skillId": ref.skill_id,
                    "versionId": ref.version_id,
                    "name": ref.name,
                    "contentHash": ref.content_hash.render(),
                }
                for ref in ordered[:128]
            ],
        }

    def search_skills(self, arguments: dict) -> dict:
        query = SkillSearchQuery.model_validate(arguments)
        if not query.query.strip():
            raise ValueError("Skill query cannot be blank")
        refs, partial = self._catalog()
        matches = match_skill_refs(list(refs.values()), query.query)
        return {
            "status": "ok",
            "truncated": partial or len(matches) > query.max_results,
            "items": [
                {
                    "skillId": match.skill.skill_id,
                    "versionId": match.skill.version_id,
                    "name": match.skill.name,
                    "contentHash": match.skill.content_hash.render(),
                    "description": match.skill.description[:8192],
                    "score": match.score,
                    "reason": match.reason,
                }
                for match in matches[: query.max_results]
            ],
        }

    def read_resource(self, arguments: dict) -> dict:
        query = SkillResourceQuery.model_validate(arguments)
        with self._lock:
            if query.skill_id not in self._selected:
                if len(self._selected) >= 32:
                    raise SkillPackageError("Activation Skill selection limit exceeded")
                refs, _ = self._catalog()
                ref = refs.get(query.skill_id)
                if ref is None:
                    raise SkillPackageError("Skill is not in the admitted catalog")
                # A download URL is obtained by the client and never retained in
                # the chosen reference or returned to consumers.
                ref = replace(ref, archive_uri="")
                package = self.store.get_cached(ref)
                if package is None:
                    package = self.store.store_archive(ref, self.client.download_skill_archive(ref))
                self._selected[query.skill_id] = ref
            return super().read_resource(arguments)

    def _packages(self):
        with self._lock:
            packages = {key: self.store.get_cached(ref) for key, ref in self._selected.items()}
            if any(package is None for package in packages.values()):
                raise SkillPackageError("Selected Skill cache integrity failed")
            return packages

    def _restore(self):
        return self.config, self._packages(), self.execution_target

    def execute(self, arguments, *, approved_selections=(), **kwargs):
        if self.execution_target is None:
            raise ValueError("Discovery execution has no admitted runtime adapter")
        query = SkillExecutionQuery.model_validate(arguments)
        approved = {
            item.skill_id: item
            for item in (
                DiscoverySkillReceipt.model_validate(value.model_dump())
                for value in approved_selections
            )
        }
        if len(approved) != len(approved_selections) or set(approved) != set(query.skill_ids):
            raise ValueError("Discovery execution requires exact committed selections")
        with self._lock:
            if any(
                key not in self._selected
                or DiscoverySkillReceipt.from_ref(self._selected[key]) != receipt
                for key, receipt in approved.items()
            ):
                raise ValueError("Discovery execution selection mismatch")
            return super().execute(arguments, **kwargs)

    def close(self):
        self.client.close()
