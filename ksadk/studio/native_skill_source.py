"""Keep Studio's Skill aliases while enforcing PluginHost's Bundle pins."""

from __future__ import annotations

from dataclasses import replace

from ksadk.harness.skill_adapters.native_bundle import NativeBundleSkillSource
from ksadk.harness.skill_runtime import SkillManifest
from ksadk.plugins.providers.harness import HarnessEngineActivationContext
from ksadk.plugins.providers.harness_contributions import ProviderSkillSource


class StudioNativeSkillSource:
    """DSH owns Skill identity and admitted text; Native consumes that snapshot.

    A model-facing alias is retained for existing disclosure receipts. Its
    mapping to each declared Bundle ref/digest is pinned in RunConfiguration.
    External inline contributions without Bundle pins retain their existing
    source contract; they are not promoted to integrity-checked packages.
    """

    def __init__(self, context: HarnessEngineActivationContext) -> None:
        self._delegate = ProviderSkillSource(context.skills)
        pinned = tuple(
            item for item in context.skills
            if item.source_ref is not None or item.content_digest is not None
        )
        self._bundle = (
            NativeBundleSkillSource(bundle_root=context.bundle.root, contributions=pinned)
            if pinned else None
        )
        self._pins = {}
        for item in pinned:
            # Reuse the existing alias projection instead of independently
            # reconstructing its name normalization or version convention.
            alias, = ProviderSkillSource((item,)).refs
            self._pins[alias] = (item.source_ref, item.content_digest)

    @property
    def refs(self) -> tuple[str, ...]:
        return self._delegate.refs

    @property
    def binding_claims(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {"model_ref": alias, "source_ref": ref, "content_digest": digest}
            for alias, (ref, digest) in self._pins.items()
        )

    def _check(self, skill_id: str) -> None:
        pin = self._pins.get(skill_id)
        if pin is not None:
            self._bundle.assert_available(pin[0])

    def manifest(self, skill_id: str) -> SkillManifest:
        self._check(skill_id)
        result = self._delegate.manifest(skill_id)
        pin = self._pins.get(skill_id)
        if pin is not None:
            result = replace(result, resource_refs=self._bundle.bound_resource_refs(pin[0]))
        self._check(skill_id)
        return result

    def full_text(self, skill_id: str) -> str:
        self._check(skill_id)
        result = self._delegate.full_text(skill_id)
        self._check(skill_id)
        return result

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        pin = self._pins.get(skill_id)
        if pin is not None:
            return self._bundle.resource(pin[0], resource_ref)
        return self._delegate.resource(skill_id, resource_ref)
