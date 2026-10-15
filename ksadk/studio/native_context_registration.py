"""Exact-Agent, trusted Python Contributor registration through PluginHost."""

from __future__ import annotations

from dataclasses import dataclass

from ksadk.plugins.composition import PluginCapabilitySelection
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.host import PluginFactory
from ksadk.plugins.providers.harness_context import HarnessContextContributorConfiguration


@dataclass(frozen=True, init=False)
class StudioNativeContextRegistration:
    _manifest_json: str
    _configuration_json: str
    factory: PluginFactory
    agent_ids: frozenset[str]

    def __init__(self, *, manifest: PluginManifest, factory: PluginFactory,
                 configuration: HarnessContextContributorConfiguration, agent_ids: frozenset[str]):
        manifest = PluginManifest.model_validate_json(manifest.model_dump_json())
        configuration = HarnessContextContributorConfiguration.model_validate_json(
            configuration.model_dump_json(),
        )
        offers = manifest.spec.provides
        if (manifest.spec.runtime != "python" or manifest.spec.isolation != "in-process"
                or len(offers) != 1 or offers[0].definition != "context.contributor/v1"
                or not isinstance(factory, PluginFactory)):
            raise ValueError("Native Context requires one registered Python capability factory")
        if not agent_ids or any(type(item) is not str or not item.strip() or item != item.strip()
                                or item == "*" for item in agent_ids):
            raise ValueError("Native Context registration requires exact Agent IDs")
        if configuration.capabilities.timeout_ms > 30_000:
            raise ValueError("Native Context deadline exceeds the source effect envelope")
        object.__setattr__(self, "_manifest_json", manifest.model_dump_json())
        object.__setattr__(self, "_configuration_json", configuration.model_dump_json())
        object.__setattr__(self, "factory", factory)
        object.__setattr__(self, "agent_ids", frozenset(agent_ids))

    @property
    def manifest(self):
        return PluginManifest.model_validate_json(self._manifest_json)

    @property
    def selection(self):
        manifest = self.manifest
        return PluginCapabilitySelection(
            ref=f"plugin://{manifest.metadata.id}@{manifest.metadata.version}",
            definition="context.contributor/v1", slot=manifest.spec.provides[0].slot,
            config=HarnessContextContributorConfiguration.model_validate_json(
                self._configuration_json,
            ).model_dump(mode="json"),
        )
