"""Consume locked Harness Hook sources under current Studio Run authority.

PluginHost owns source runtimes. This Port owns only callback Tasks; a selected
source is not an authorization grant and a Hook output is never a verifier.
"""

from __future__ import annotations

import asyncio
import inspect

from ksadk.harness.native.hook_executor import HookAuthorizer, HookBinding, ScopedHookExecutor
from ksadk.harness.native.hook_runtime_contract import (
    HookInvocationRequest,
    HookInvocationResult,
    _json_bytes,
)
from ksadk.harness.native.state import RunPhase, RunScope
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.harness.stores.native_contract import RunStore
from ksadk.plugins.contracts import plugin_lock_digest
from ksadk.plugins.providers.harness import HarnessEngineActivationContext
from ksadk.plugins.providers.harness_hooks import hook_configuration
from ksadk.plugins.resolver import composition_profile_digest
from ksadk.studio.native_hook_registration import validate_hook_authorization_policy
from ksadk.studio.native_scope import native_scope_from_activation


class StudioNativeHookPort:
    def __init__(
        self, context: HarnessEngineActivationContext, scope: RunScope, store: RunStore,
        *, authorize: HookAuthorizer | None = None, authorization_version: str | None = None,
    ):
        if native_scope_from_activation(context) != scope or scope.agent_id != context.agent_name:
            raise ValueError("native_hook_scope_unbound")
        if not context.activation_generation or context.activation_available is None:
            raise ValueError("native_hook_activation_unbound")
        if (composition_profile_digest(context.bundle.composition.profile),
                plugin_lock_digest(context.bundle.composition.plugin_lock)) != (
            context.profile_digest, context.plugin_lock_digest,
        ):
            raise ValueError("native_hook_graph_unbound")
        validate_hook_authorization_policy(authorize, authorization_version)
        self._context, self._scope, self._store = context, scope.model_copy(deep=True), store
        self._manifest = context.bundle.manifest.model_dump_json()
        self._profile = context.bundle.composition.profile.model_dump_json()
        self._lock = context.bundle.composition.plugin_lock.model_dump_json()
        self._closed = False
        self._executors = {}
        self._definitions = []
        stop_limits = set()
        maximum_deadline = 0.0

        async def admitted(request):
            self._ensure_current(request)
            decision = await authorize(request)  # Only constructed when a host policy exists.
            self._ensure_current(request)
            return decision

        for source in context.hook_sources:
            config = hook_configuration(context.bundle, source)
            ref = f"plugin://{source.plugin_id}@{source.plugin_version}"
            definitions = config.definitions(source_ref=ref, binding_digest=studio_mapping_digest({
                "bundle_digest": context.bundle.bundle_digest,
                "profile_digest": context.profile_digest,
                "plugin_lock_digest": context.plugin_lock_digest,
                "source_ref": ref, "slot": source.slot,
                "policy": "studio.native-locked-hook-callback/v1",
                "authorization_version": authorization_version or "deny-unconfigured/v1",
                "configuration": config.model_dump(mode="json"),
            }))
            if config.stop_continuation_limit is not None:
                stop_limits.add(config.stop_continuation_limit)
            read = source.runtime.harness_hook  # Freeze the selected implementation, not its name.
            if not inspect.iscoroutinefunction(read):
                raise ValueError("native_hook_producer_invalid")

            async def callback(request, read=read):
                self._ensure_current(request)
                output = await read(context.bundle, request)
                self._ensure_current(request)
                return output

            executor = ScopedHookExecutor(
                scope=self._scope,
                bindings=tuple(
                    HookBinding(item, callback, self._available) for item in definitions
                ),
                authorize=admitted if authorize is not None else None,
                cleanup_timeout_seconds=config.cleanup_timeout_seconds,
            )
            for item in definitions:
                if item.hook_id in self._executors:
                    raise ValueError("native_hook_id_duplicate")
                self._executors[item.hook_id] = executor
                self._definitions.append(item)
                maximum_deadline = max(
                    maximum_deadline, item.timeout_seconds + config.cleanup_timeout_seconds,
                )
        if len(stop_limits) > 1:
            raise ValueError("native_hook_stop_policy_conflict")
        self.stop_continuation_limit = next(iter(stop_limits), None)
        self.required_effect_timeout_seconds = maximum_deadline

    @property
    def definitions(self):
        return tuple(item.model_copy(deep=True) for item in self._definitions)

    def _available(self):
        return (
            not self._closed and self._context.activation_available() is True
            and self._context.bundle.manifest.model_dump_json() == self._manifest
            and self._context.bundle.composition.profile.model_dump_json() == self._profile
            and self._context.bundle.composition.plugin_lock.model_dump_json() == self._lock
        )

    def _ensure_current(self, request):
        if request.scope != self._scope or request.session_id != self._context.activation_key:
            raise ValueError("native_hook_request_unbound")
        snapshot = self._store.load(self._scope, request.run_id)
        intent = self._store.get_intent(self._scope, request.run_id, request.operation_key)
        expected_phase = (RunPhase.VERIFYING if request.event == "Stop" else RunPhase.COMPACTING
                          if request.event == "PreCompact" else RunPhase.EXECUTING_TOOLS)
        if (
            snapshot.session_id != request.session_id or snapshot.turn_id != request.turn_id
            or snapshot.goal != request.goal or snapshot.goal_revision != request.goal_revision
            or snapshot.tool_calls != request.tool_call_count or snapshot.phase != expected_phase
            or snapshot.pending_intent != request.operation_key
            or intent is None or intent.kind != "hook" or intent.status != "running"
            or _json_bytes(intent.payload.get("request"))
            != _json_bytes(request.model_dump(mode="json"))
        ):
            raise ValueError("native_hook_request_unbound")

    async def invoke(self, request: HookInvocationRequest) -> HookInvocationResult:
        request = HookInvocationRequest.model_validate_json(request.model_dump_json())
        self._ensure_current(request)
        executor = self._executors.get(request.definition.hook_id)
        if executor is None:
            raise ValueError("native_hook_definition_unbound")
        return await executor.invoke(request)

    async def aclose(self):
        self._closed = True
        await asyncio.gather(*(item.aclose() for item in set(self._executors.values())))
