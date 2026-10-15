"""Optional completed-Turn consumption by the selected DSH Memory owner.

The Native adapter's owned close task awaits this deterministic stage after
terminal commit. It is not an auxiliary model or a second Run state machine.
Only the owning Memory Provider's receipts prove writes; a failure here cannot
replace the main task's terminal, final answer, usage or verification evidence.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import Counter

from ksadk.harness.native.completion import TaskContract, VerificationReport
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.state import RunPhase
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.memory.extraction import NATIVE_USER_EXTRACTION_POLICY
from ksadk.plugins.memory_provider import MemoryFlushRequest, MemoryPrincipal
from ksadk.studio.contracts import ContextSpec, MemorySpec
from ksadk.studio.native_memory_acceptance import VERIFIER_REF, VERIFIER_VERSION
from ksadk.studio.native_memory_context import selected_memory_binding
from ksadk.studio.native_memory_sources import memory_bundle_snapshot, original_user_memory_sources
from ksadk.studio.native_memory_tool_port import memory_receipt_request
from ksadk.studio.native_scope import native_scope_from_activation

_LOG = logging.getLogger(__name__)


def completed_turn_memory_eligible(snapshot) -> bool:
    """Original user observations do not require semantic proof of the answer.

    Keep failed/blocked verification conservative. A host that cannot verify
    an ordinary answer may still consume its independently pinned user input.
    The Memory owner continues to decide candidate, shadow and write policy.
    """
    if snapshot.outcome == "verified":
        return True
    if snapshot.outcome != "unverified":
        return False
    if snapshot.reason in {"required_acceptance_empty", "verification_report_missing"}:
        return True
    if snapshot.reason != "required_verification_incomplete" or snapshot.verification is None:
        return False
    report = VerificationReport.model_validate(snapshot.verification)
    return not any(item.status in {"failed", "blocked"} for item in report.items)


class NativeMemoryTurnConsumer:
    def __init__(self, context, scope, store, *, profile, effect_timeout_seconds,
                 mutation_definition=None):
        if native_scope_from_activation(context) != scope:
            raise ValueError("native_memory_turn_scope_unbound")
        binding, ref = selected_memory_binding(context)
        if not all(inspect.iscoroutinefunction(getattr(binding.runtime, method, None))
                   for method in ("memory_flush", "memory_receipt")):
            raise ValueError("native_memory_turn_binding_invalid")
        self._context, self._scope, self._store = context, scope, store
        self._owner = binding.runtime
        self._bundle = memory_bundle_snapshot(context)
        self._mutation = mutation_definition.model_copy(deep=True) if mutation_definition else None
        self._rollout = ContextSpec.model_validate(
            context.bundle.resolved_agent_spec.get("context") or {},
        ).rollout.memory_write
        self._timeout = float(effect_timeout_seconds) / 2
        self._max_input_bytes = profile.context_window * 8
        self.binding_digest = studio_mapping_digest({
            "bundle": context.bundle.bundle_digest, "profile": context.profile_digest,
            "lock": context.plugin_lock_digest, "source": ref, "slot": binding.slot,
            "memory": MemorySpec.model_validate(
                context.bundle.resolved_agent_spec.get("memory") or {},
            ).model_dump(mode="json"),
            "rollout": self._rollout, "extraction": NATIVE_USER_EXTRACTION_POLICY,
            "policy": "studio-scoped-completed-turn-memory/v1",
            "deadline_seconds": self._timeout, "max_input_bytes": self._max_input_bytes,
        })

    def _current(self, snapshot):
        if (snapshot.scope != self._scope or snapshot.phase != RunPhase.COMPLETED
                or snapshot.session_id != self._context.activation_key
                or self._context.activation_available() is not True
                or memory_bundle_snapshot(self._context) != self._bundle
                or self._store.load(self._scope, snapshot.run_id) != snapshot):
            raise ValueError("native_memory_turn_snapshot_unbound")

    async def _direct_write(self, snapshot):
        if self._mutation is None or self._rollout != "enabled":
            return None
        for message in snapshot.messages:
            if (message.get("role") != "tool"
                    or message.get("source_run_id", snapshot.run_id) != snapshot.run_id):
                continue
            key = message.get("receipt_ref")
            if not isinstance(key, str):
                continue
            intent = self._store.get_intent(self._scope, snapshot.run_id, key)
            receipt = self._store.get_receipt(self._scope, snapshot.run_id, key)
            if intent is None or intent.kind != "tool":
                continue
            raw_call = intent.payload.get("call")
            if not isinstance(raw_call, dict) or raw_call.get("name") != self._mutation.name:
                continue
            call = ModelToolCall.model_validate(raw_call)
            if call.call_id != message.get("tool_call_id"):
                continue
            if (receipt is None or receipt.status != "committed"
                    or receipt.result.get("status") != "ok"):
                # A denied/failed manual operation is not permission to save
                # the same original input via automatic candidate extraction.
                return "memory_mutation_not_confirmed"
            if "hook_effective_arguments" in message:
                call = call.model_copy(update={"arguments": message["hook_effective_arguments"]})
            # A tool name or a JSON "committed" claim is insufficient. Confirm
            # the exact scoped call and locked definition with its actual owner.
            persisted = await self._owner.memory_receipt(
                self._context.bundle,
                memory_receipt_request(self._scope, key, call, self._mutation),
            )
            if persisted is not None and persisted.get("committed") is True:
                return "direct_write_already_committed"
            return "memory_mutation_not_confirmed"
        return None

    async def _consume(self, snapshot):
        self._current(snapshot)
        contract = TaskContract.model_validate(snapshot.task_contract)
        if any(item.verifier_ref == VERIFIER_REF and item.verifier_version == VERIFIER_VERSION
               for item in contract.items):
            # Explicit Memory tasks already have an independent persistence
            # owner and repair loop. Automatic extraction cannot manufacture a
            # duplicate Profile from an existing Fact, or bypass its refusal.
            return {"status": "explicit_memory_task_owned"}
        if not completed_turn_memory_eligible(snapshot):
            return {"status": "main_task_not_qualified"}
        observations, unavailable = original_user_memory_sources(
            self._store, self._scope, snapshot.run_id,
            ((index, message) for index, message in enumerate(snapshot.messages)
             if message.get("source_run_id", snapshot.run_id) == snapshot.run_id),
        )
        if not observations:
            return {"status": "empty", "source_unavailable": unavailable}
        if sum(len(item.content.encode()) for item in observations) > self._max_input_bytes:
            return {"status": "input_over_budget"}
        if direct_write := await self._direct_write(snapshot):
            return {"status": direct_write}
        result = await self._owner.memory_flush(self._context.bundle, MemoryFlushRequest(
            principal=MemoryPrincipal(**self._scope.model_dump()), trigger="completed_turn",
            observations=observations,
        ))
        self._current(snapshot)
        return {"status": result["status"], "proposed": result["proposed"],
                "decisions": dict(Counter(item["decision"] for item in result["results"])),
                "source_unavailable": unavailable}

    async def __call__(self, snapshot):
        try:
            async with asyncio.timeout(self._timeout):
                receipt = await self._consume(snapshot)
        except Exception:
            # The Provider retains shielded database work and durable source
            # receipts. No blind retry or false assertion that nothing wrote.
            _LOG.warning("Native completed-Turn Memory outcome unknown",
                         extra={"run_id": snapshot.run_id, "trigger": "completed_turn"})
            return
        _LOG.info("Native completed-Turn Memory evaluated",
                  extra={"run_id": snapshot.run_id, "memory_receipt": receipt})


def build_native_memory_turn_consumer(context, scope, store, *, profile,
                                     effect_timeout_seconds, mutation_definition=None):
    memory = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    rollout = ContextSpec.model_validate(
        context.bundle.resolved_agent_spec.get("context") or {},
    ).rollout.memory_write
    if (not memory.enabled or memory.provider_ref != "local-default" or "user" not in memory.scopes
            or memory.write.mode == "off" or not memory.write.flush_after_turn or rollout == "off"):
        return None
    return NativeMemoryTurnConsumer(
        context, scope, store, profile=profile, effect_timeout_seconds=effect_timeout_seconds,
        mutation_definition=mutation_definition,
    )
