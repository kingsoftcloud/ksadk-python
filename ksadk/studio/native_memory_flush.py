"""Consume canonical user history through the locked DSH Memory owner.

No auxiliary model, global Provider resolver or transcript-derived authority.
The Kernel's generic Hook effect owns dispatch; the selected Memory runtime
owns database threads and per-original-source atomic candidate receipts.
"""

from __future__ import annotations

import inspect
import json
from collections import Counter

from ksadk.harness.native.hook_executor import HookBinding, ScopedHookExecutor
from ksadk.harness.native.hook_runtime_contract import (
    HookDefinition,
    HookOutput,
    _json_bytes,
    hook_execution_policy_digest,
)
from ksadk.harness.native.state import RunPhase
from ksadk.plugins.memory_provider import MemoryFlushRequest, MemoryPrincipal
from ksadk.studio.contracts import ContextSpec, MemorySpec
from ksadk.studio.native_memory_context import selected_memory_binding
from ksadk.studio.native_memory_sources import memory_bundle_snapshot, original_user_memory_sources
from ksadk.studio.native_scope import native_scope_from_activation


class _MemoryFlushPort:
    stop_continuation_limit = None

    def __init__(self, context, scope, store, *, profile, effect_timeout_seconds):
        from ksadk.harness.native.studio_admission import studio_mapping_digest
        from ksadk.memory.extraction import NATIVE_USER_EXTRACTION_POLICY

        if native_scope_from_activation(context) != scope:
            raise ValueError("native_memory_flush_scope_unbound")
        binding, ref = selected_memory_binding(context)
        self._context, self._scope, self._store = context, scope, store
        self._runtime = binding.runtime
        if not inspect.iscoroutinefunction(getattr(self._runtime, "memory_flush", None)):
            raise ValueError("native_memory_flush_binding_invalid")
        self._callback = self._runtime.memory_flush
        self._bundle = self._bundle_snapshot()
        self._closed = False
        memory = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
        rollout = ContextSpec.model_validate(
            context.bundle.resolved_agent_spec.get("context") or {},
        ).rollout.memory_write
        # Compile explicit bounded callback/cleanup deadlines within the
        # existing parent effect budget. This stage performs no model request.
        deadline, cleanup = float(effect_timeout_seconds) / 2, float(effect_timeout_seconds) / 4
        definition = HookDefinition(
            hook_id="ksadk.memory.precompact", source_ref=ref,
            binding_digest=studio_mapping_digest({
                "bundle": context.bundle.bundle_digest, "profile": context.profile_digest,
                "lock": context.plugin_lock_digest, "source": ref, "slot": binding.slot,
                "memory": memory.model_dump(mode="json"), "rollout": rollout,
                "policy": "studio-scoped-original-user-memory/v1",
                "extraction": NATIVE_USER_EXTRACTION_POLICY,
            }),
            execution_policy_digest=hook_execution_policy_digest(cleanup_timeout_seconds=cleanup),
            events=("PreCompact",), compaction_source_projection="canonical_user",
            read_only=rollout == "shadow", timeout_seconds=deadline,
            # Byte exposure budget, not a tokenizer estimate. Canonical user
            # input only; archived tool bodies/images are never sent to Memory.
            max_input_bytes=profile.context_window * 8, max_output_bytes=16384,
            failure_mode="warn",
        )
        self.definitions = (definition,)
        self.required_effect_timeout_seconds = deadline + cleanup
        self._executor = ScopedHookExecutor(
            scope=scope, bindings=(HookBinding(definition, self._produce, self._available),),
            authorize=self._authorize, cleanup_timeout_seconds=cleanup,
        )

    def _available(self):
        return (not self._closed and self._context.activation_available() is True
                and self._bundle_snapshot() == self._bundle)

    def _bundle_snapshot(self):
        return memory_bundle_snapshot(self._context)

    def _current(self, request):
        snapshot = self._store.load(self._scope, request.run_id)
        intent = self._store.get_intent(self._scope, request.run_id, request.operation_key)
        if (
            not self._available() or request.scope != self._scope
            or request.session_id != self._context.activation_key
            or snapshot.phase != RunPhase.COMPACTING or snapshot.turn_id != request.turn_id
            or (snapshot.goal, snapshot.goal_revision) != (request.goal, request.goal_revision)
            or snapshot.pending_intent != request.operation_key
            or intent is None or intent.status != "running" or intent.kind != "hook"
            or _json_bytes(intent.payload.get("request"))
            != _json_bytes(request.model_dump(mode="json"))
            or request.definition != self.definitions[0]
        ):
            raise ValueError("native_memory_flush_request_unbound")
        return snapshot

    async def _authorize(self, request):
        self._current(request)
        # The immutable MemorySpec/rollout authorizes evaluation, not arbitrary
        # writes. The Provider still enforces source classification and Policy.
        return "allow"

    async def _produce(self, request):
        self._current(request)
        observations, unavailable = original_user_memory_sources(
            self._store, self._scope, request.run_id,
            ((int(ref.split(":", 2)[1]), raw) for ref, raw in zip(
                request.compaction.message_refs, request.compaction.messages, strict=True,
            )),
        )
        result = await self._callback(self._context.bundle, MemoryFlushRequest(
            principal=MemoryPrincipal(**self._scope.model_dump()), trigger="pre_compaction",
            observations=observations,
        ))
        self._current(request)
        # Evaluation receipts are historical effects, never proof of the
        # current fact after another connection changes/forgets it.
        return HookOutput(additional_context=(json.dumps({
            "memory_candidate_flush": result["status"], "proposed": result["proposed"],
            "decisions": dict(Counter(item["decision"] for item in result["results"])),
            "source_unavailable": unavailable, "current_fact_verification": False,
            "trust_level": "untrusted",
        }, ensure_ascii=False, sort_keys=True),))

    async def invoke(self, request):
        self._current(request)
        return await self._executor.invoke(request)

    async def aclose(self):
        self._closed = True
        await self._executor.aclose()


class _HookMux:
    def __init__(self, ports):
        self._ports = tuple(ports)
        self.definitions = tuple(item for port in self._ports for item in port.definitions)
        self._routes = {item.hook_id: port for port in self._ports for item in port.definitions}
        if len(self._routes) != len(self.definitions):
            raise ValueError("native_hook_id_duplicate")
        limits = {port.stop_continuation_limit for port in self._ports
                  if port.stop_continuation_limit is not None}
        if len(limits) > 1:
            raise ValueError("native_hook_stop_policy_conflict")
        self.stop_continuation_limit = next(iter(limits), None)
        self.required_effect_timeout_seconds = max(
            port.required_effect_timeout_seconds for port in self._ports
        )

    async def invoke(self, request):
        return await self._routes[request.definition.hook_id].invoke(request)

    async def aclose(self):
        for port in reversed(self._ports):
            await port.aclose()


def with_native_memory_flush(context, scope, store, *, profile, effect_timeout_seconds,
                             hook_port=None):
    memory = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    rollout = ContextSpec.model_validate(
        context.bundle.resolved_agent_spec.get("context") or {},
    ).rollout.memory_write
    if (not memory.enabled or memory.provider_ref != "local-default" or "user" not in memory.scopes
            or memory.write.mode == "off" or not memory.write.flush_before_compaction
            or rollout == "off"):
        return hook_port
    owned = _MemoryFlushPort(context, scope, store, profile=profile,
                            effect_timeout_seconds=effect_timeout_seconds)
    return _HookMux((hook_port, owned)) if hook_port is not None else owned
