"""Trusted distribution selection of a Python Hook capability for exact Agents.

These inputs come from the Studio host, not an Agent draft or a tool response.
PluginHost still stages the registered factory and checks declared permissions.
This does not add a plugin installer, Cordis Hook export or management API.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from ksadk.harness.native.hook_executor import HookAuthorizer
from ksadk.plugins.composition import PluginCapabilitySelection
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.host import PluginFactory
from ksadk.plugins.providers.harness_hooks import HARNESS_HOOK_DEFINITION, HarnessHookConfiguration


def validate_hook_authorization_policy(authorize: HookAuthorizer | None, version: str | None):
    if (authorize is None) != (version is None) or (authorize is not None and (
        not (inspect.iscoroutinefunction(authorize)
             or inspect.iscoroutinefunction(getattr(authorize, "__call__", None)))
        or type(version) is not str or not version.strip()
    )):
        raise ValueError("native_hook_authorization_policy_invalid")


@dataclass(frozen=True, init=False)
class StudioNativeHookRegistration:
    _manifest_json: str
    _configuration_json: str
    factory: PluginFactory
    agent_ids: frozenset[str]

    def __init__(self, *, manifest: PluginManifest, factory: PluginFactory,
                 configuration: HarnessHookConfiguration, agent_ids: frozenset[str]):
        manifest = PluginManifest.model_validate_json(manifest.model_dump_json())
        configuration = HarnessHookConfiguration.model_validate_json(
            configuration.model_dump_json(),
        )
        offers = manifest.spec.provides
        if (manifest.spec.runtime != "python" or manifest.spec.isolation != "in-process"
                or len(offers) != 1 or offers[0].definition != HARNESS_HOOK_DEFINITION
                or not isinstance(factory, PluginFactory)):
            raise ValueError("Native Hook requires one registered Python capability factory")
        if not agent_ids or any(type(item) is not str or not item.strip() or item != item.strip()
                                or item == "*" for item in agent_ids):
            raise ValueError("Native Hook registration requires exact Agent IDs")
        ref = f"plugin://{manifest.metadata.id}@{manifest.metadata.version}"
        configuration.definitions(source_ref=ref, binding_digest=manifest.spec.provenance.digest)
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
            definition=HARNESS_HOOK_DEFINITION, slot=manifest.spec.provides[0].slot,
            config=HarnessHookConfiguration.model_validate_json(
                self._configuration_json,
            ).model_dump(mode="json"),
        )
