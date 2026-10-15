"""Locked PluginHost Hook sources; configuration proposes no execution grant.

This is the Python capability boundary for the DSH-backed Harness provider.
It does not interpret Cordis/MCP exports as Hooks or execute shell/HTTP config.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from ksadk.harness.native.hook_runtime_contract import (
    HookDefinition,
    HookInvocationRequest,
    HookOutput,
    hook_execution_policy_digest,
)
from ksadk.harness.native.state import Contract
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.host import PluginExecutionContext, PluginHostError

HARNESS_HOOK_DEFINITION = "harness.hook/v1"


class HarnessHookConfiguration(Contract):
    schema_version: Literal["ksadk.harness-hook-binding/v1"]
    hooks: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=16)
    cleanup_timeout_seconds: float = Field(strict=True, gt=0, allow_inf_nan=False)
    stop_continuation_limit: int | None = Field(default=None, strict=True, ge=0)

    def definitions(self, *, source_ref: str, binding_digest: str) -> tuple[HookDefinition, ...]:
        policy = hook_execution_policy_digest(
            cleanup_timeout_seconds=self.cleanup_timeout_seconds,
        )
        definitions = []
        for declaration in self.hooks:
            # Resource identity is derived from the locked host graph. Neither
            # editable config nor a callback may replace it.
            if {"source_ref", "binding_digest", "execution_policy_digest"} & declaration.keys():
                raise ValueError("Hook config cannot supply binding authority")
            definitions.append(HookDefinition(
                **declaration, source_ref=source_ref, binding_digest=binding_digest,
                execution_policy_digest=policy,
            ))
        if len({item.hook_id for item in definitions}) != len(definitions):
            raise ValueError("duplicate Hook declarations")
        has_stop = any("Stop" in item.events for item in definitions)
        if has_stop != (self.stop_continuation_limit is not None):
            raise ValueError("Stop Hook requires its own explicit continuation policy")
        return tuple(definitions)


@runtime_checkable
class HarnessHookSource(Protocol):
    async def harness_hook(
        self, bundle: ResolvedPluginBundle, request: HookInvocationRequest,
    ) -> HookOutput: ...


@dataclass(frozen=True)
class HarnessHookBinding:
    plugin_id: str
    plugin_version: str
    slot: str
    runtime: HarnessHookSource


def hook_configuration(
    bundle: ResolvedPluginBundle, binding: HarnessHookBinding,
) -> HarnessHookConfiguration:
    ref = f"plugin://{binding.plugin_id}@{binding.plugin_version}"
    selected = [item for item in bundle.composition.profile.capabilities if item.ref == ref]
    locked = [item for item in bundle.composition.plugin_lock.plugins
              if (item.id, item.version) == (binding.plugin_id, binding.plugin_version)]
    if len(selected) != 1 or len(locked) != 1 or not any(
        (item.definition, item.slot, item.owner)
        == (HARNESS_HOOK_DEFINITION, binding.slot, binding.plugin_id)
        for item in locked[0].provides
    ):
        raise ValueError("native_hook_source_unbound")
    config = HarnessHookConfiguration.model_validate_json(
        HarnessHookConfiguration.model_validate(selected[0].config).model_dump_json(),
    )
    config.definitions(source_ref=ref, binding_digest=locked[0].digest)
    return config


def project_harness_hooks(
    bundle: ResolvedPluginBundle, capabilities: PluginExecutionContext,
) -> tuple[HarnessHookBinding, ...]:
    selected = capabilities.all(HARNESS_HOOK_DEFINITION)
    if not selected:
        return ()
    if (capabilities.profile_digest, capabilities.plugin_lock_digest) != (
        bundle.composition.profile_digest, bundle.composition.plugin_lock_digest,
    ):
        raise PluginHostError("harness_hook_binding_invalid", "Hook graph pins differ from Bundle")
    bindings = []
    owners = set()
    for item in selected:
        if not isinstance(item.runtime, HarnessHookSource) or not inspect.iscoroutinefunction(
            item.runtime.harness_hook,
        ):
            raise PluginHostError(
                "harness_hook_incompatible", "Hook source requires an async producer",
            )
        if item.plugin_id in owners:
            raise PluginHostError("harness_hook_binding_invalid", "duplicate Hook owner")
        owners.add(item.plugin_id)
        binding = HarnessHookBinding(item.plugin_id, item.plugin_version, item.slot, item.runtime)
        try:
            hook_configuration(bundle, binding)
        except (ValueError, TypeError) as error:
            raise PluginHostError(
                "harness_hook_binding_invalid", "Hook source or policy is not locked",
            ) from error
        bindings.append(binding)
    return tuple(bindings)
