"""Host-owned checks of explicit durable Memory goals, outside the Kernel.

An assistant promise is not a persisted fact. Reuse the locked DSH owner and
Native verification/repair protocol without authorizing writes here.
"""

from __future__ import annotations

from ksadk.harness.native.completion import TaskContract, VerificationItemResult, VerificationReport
from ksadk.harness.native.context_contributions import ContextCollectionResult
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import VerificationBundle
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.memory.extraction import explicit_memory_target
from ksadk.memory.models import MemoryCandidate
from ksadk.memory.policy import MemoryPolicy, content_hash
from ksadk.plugins.memory_provider import MemoryPrincipal, MemoryRecordRequest
from ksadk.studio.contracts import ContextSpec, MemorySpec
from ksadk.studio.native_memory_context import _time, selected_memory_binding
from ksadk.studio.native_memory_tool_port import memory_receipt_request

VERIFIER_REF = "studio.explicit-durable-memory"
VERIFIER_VERSION = "2"


def extend_memory_goal(goal, contract, *, provider_ref):
    """Compile a known single operation; leave other semantics to their owner."""
    target = explicit_memory_target(goal)
    if (
        provider_ref != "local-default"
        or target is None
        or len(contract.items) != 1
        or contract.final_submission_schema is not None
    ):
        return contract
    item = contract.items[0]
    if item.acceptance_id != "current_user_goal":
        return contract
    identity = studio_mapping_digest({"target": target, "verifier": VERIFIER_VERSION})[7:27]
    return contract.model_copy(
        update={
            "revision": f"{contract.revision}+memory-{identity}",
            "items": (
                item.model_copy(
                    update={
                        "verifier_ref": VERIFIER_REF,
                        "verifier_version": VERIFIER_VERSION,
                        "verification_method": "deterministic",
                    }
                ),
            ),
        }
    )


class StudioNativeMemoryVerifier:
    def __init__(self, *, context, scope, store, baseline, definitions):
        self.context, self.scope, self.store, self.baseline = context, scope, store, baseline
        self.policy = MemorySpec.model_validate(
            context.bundle.resolved_agent_spec.get("memory") or {}
        )
        self.rollout = ContextSpec.model_validate(
            context.bundle.resolved_agent_spec.get("context") or {},
        ).rollout.memory_write
        self.definitions = {item.name: item for item in definitions}
        self.principal = MemoryPrincipal(**scope.model_dump())
        self.runtime, self.ref = None, None
        if (
            self.policy.enabled
            and self.policy.provider_ref == "local-default"
            and self.policy.scopes
        ):
            binding, self.ref = selected_memory_binding(context)
            self.runtime = binding.runtime

    async def verify(self, request):
        selected = [item for item in request.contract.items if item.verifier_ref == VERIFIER_REF]
        if not selected:
            return await self.baseline.verify(request)
        if request.scope != self.scope:
            raise ValueError("native_memory_verification_scope_mismatch")
        snapshot = self.store.load(self.scope, request.run_id)
        target = explicit_memory_target(snapshot.goal)
        pinned = TaskContract.model_validate(snapshot.task_contract)
        owned = next(
            (item for item in pinned.items if item.acceptance_id == "current_user_goal"), None
        )
        if (
            len(selected) != 1
            or target is None
            or selected[0].acceptance_id != "current_user_goal"
            or selected[0].verifier_version != VERIFIER_VERSION
            or selected[0].verification_method != "deterministic"
            or selected[0] != owned
            or request.contract.revision != pinned.revision
            or any(item not in pinned.items for item in request.contract.items)
        ):
            raise ValueError("native_memory_verification_contract_mismatch")
        # The original verifier still supplies the candidate and environment
        # binding. It cannot approve this new persistence acceptance.
        base_items = tuple(item for item in request.contract.items if item not in selected)
        bundle = await self.baseline.verify(
            request.model_copy(
                update={
                    "contract": request.contract.model_copy(update={"items": base_items}),
                }
            )
        )
        try:
            status, reason, references = await self._observe(snapshot, target)
        except Exception:  # Provider failure is unknown state, never an approval.
            status, reason, references = "blocked", "memory_verification_unavailable", ()
        summary = (
            "The exact requested fact is present in the current scoped durable Memory."
            if status == "passed"
            else "Long-term Memory persistence cannot be confirmed: " + reason + "."
            if status == "blocked"
            else "The requested durable fact is not present. Use the bound manage_memory tool "
            "to save it; for a correction read its current memory_id/version and correct that "
            "record. Inspect the actual result before finishing."
        )
        observed_ref = "memory-check:" + studio_mapping_digest(
            {
                "scope": self.scope.model_dump(mode="json"),
                "run": snapshot.run_id,
                "goal_revision": snapshot.goal_revision,
                "target_hash": content_hash(target[1]),
                "status": status,
                "reason": reason,
            }
        )
        result = VerificationItemResult(
            acceptance_id=selected[0].acceptance_id,
            status=status,
            verifier_id=VERIFIER_REF,
            verifier_version=VERIFIER_VERSION,
            verification_method="deterministic",
            evidence_refs=(*references, observed_ref),
            safe_summary=summary,
            repairable=status == "failed",
        )
        results = list(bundle.report.items) if bundle.report is not None else []
        covered = {item.acceptance_id for item in results}
        results.extend(
            VerificationItemResult(
                acceptance_id=item.acceptance_id,
                status="not_run",
                verifier_id=item.verifier_ref,
                verifier_version=item.verifier_version,
                verification_method=item.verification_method,
                evidence_refs=(),
            )
            for item in base_items
            if item.acceptance_id not in covered
        )
        results.append(result)
        candidate = bundle.candidate.model_copy(
            update={
                "environment_digest": studio_mapping_digest(
                    {
                        "baseline": bundle.candidate.environment_digest,
                        "bundle": self.context.bundle.bundle_digest,
                        "provider": self.ref,
                        "policy": self.policy.model_dump(mode="json"),
                        "rollout": self.rollout,
                        "verifier": VERIFIER_VERSION,
                    }
                ),
            }
        )
        report = VerificationReport(
            candidate_id=candidate.candidate_id,
            contract_revision=request.contract.revision,
            subject_digest=candidate.subject_digest,
            subject_context_digest=candidate.subject_context_digest,
            environment_digest=candidate.environment_digest,
            items=tuple(results),
            repairable=status == "failed" or bool(bundle.report and bundle.report.repairable),
            failure_fingerprint=studio_mapping_digest(
                {
                    "verifier": VERIFIER_REF,
                    "contract": request.contract.revision,
                    "status": status,
                    "reason": reason,
                    "baseline": bundle.report.failure_fingerprint if bundle.report else "",
                }
            )
            if status != "passed"
            else "",
        )
        return VerificationBundle(
            candidate=candidate, report=report, post_workspace=bundle.post_workspace
        )

    async def _observe(self, snapshot, target):
        if self.runtime is None:
            return "blocked", "memory_not_enabled_in_build", ()
        operation, body = target
        # An already persisted, recalled add target needs no duplicate write.
        # A correction still needs the original record's real CAS operation.
        if operation == "add" and snapshot.context_source_receipt is not None:
            collection = ContextCollectionResult.model_validate(snapshot.context_source_receipt)
            if collection.goal_revision == snapshot.goal_revision:
                for source in collection.sources:
                    if (
                        source.source_ref != self.ref
                        or source.status != "ok"
                        or source.contribution is None
                    ):
                        continue
                    for fragment in source.contribution.fragments:
                        memory_id = fragment.metadata.get("memory_id")
                        if fragment.content == body and isinstance(memory_id, str):
                            if record := await self._current(memory_id, body):
                                return "passed", "current_durable_fact", (self._record_ref(record),)
        observations = tuple(self._tool_observations(snapshot.run_id))
        if operation == "add":
            for event, key, receipt, call, result in observations:
                if (
                    call.name in {"read_memory", "search_memory"}
                    and receipt.status == "committed"
                    and result.get("status") == "ok"
                ):
                    if call.name == "read_memory":
                        identities = (call.arguments.get("memory_id"),)
                    else:
                        content = result.get("content")
                        records = content.get("records") if isinstance(content, dict) else None
                        identities = (
                            tuple(
                                item.get("memory_id")
                                for item in records
                                if isinstance(item, dict) and item.get("content") == body
                            )
                            if isinstance(records, list)
                            else ()
                        )
                    for memory_id in identities:
                        if isinstance(memory_id, str):
                            if record := await self._current(memory_id, body):
                                return (
                                    "passed",
                                    "current_read_fact",
                                    ("native-event:" + event.event_id, self._record_ref(record)),
                                )
        definition = self.definitions.get("manage_memory")
        if definition is None or self.policy.write.mode == "off" or self.rollout != "enabled":
            return "blocked", "memory_write_not_committing", ()
        if len(body) > 8192:
            return "blocked", "memory_fact_exceeds_tool_limit", ()
        evaluation = MemoryPolicy().evaluate(
            MemoryCandidate(
                candidate_id="verification-only",
                operation=operation,
                memory_type="profile",
                scope="user",
                scope_id="",
                content=body,
                confidence=1,
                importance=0.8,
                source_event_ids=[],
                reason="explicit_user_request",
            )
        )
        if evaluation.decision != "commit":
            return "blocked", evaluation.reason, ()
        references = []
        for event, key, receipt, call, result in observations:
            if call.name != "manage_memory":
                continue
            if call.arguments.get("content") != body or call.arguments.get("action") != (
                "remember" if operation == "add" else "correct"
            ):
                continue
            references.append("native-event:" + event.event_id)
            content = result.get("content")
            if not isinstance(content, dict):
                continue
            if content.get("code") == "policy_denied":
                return "blocked", "memory_approval_denied", tuple(references)
            if (
                receipt.status != "committed"
                or result.get("status") != "ok"
                or content.get("committed") is not True
            ):
                continue
            actual = await self.runtime.memory_receipt(
                self.context.bundle,
                memory_receipt_request(self.scope, key, call, definition),
            )
            if actual != content or not isinstance(actual.get("memory_id"), str):
                continue
            if record := await self._current(actual["memory_id"], body):
                return "passed", "current_committed_fact", (*references, self._record_ref(record))
        return "failed", "durable_fact_missing", tuple(references)

    def _tool_observations(self, run_id):
        """Inspect sealed original receipts, including readonly batch members."""
        for event in self.store.events(self.scope, run_id):
            if event.kind != "effect.received" or event.payload.get("kind") != "tool":
                continue
            key = event.payload.get("operation_key")
            if not isinstance(key, str):
                continue
            intent = self.store.get_intent(self.scope, run_id, key)
            receipt = self.store.get_receipt(self.scope, run_id, key)
            if intent is None or receipt is None or intent.kind != "tool":
                continue
            calls = intent.payload.get("calls")
            if isinstance(calls, list):
                content = receipt.result.get("content")
                results = content.get("batch_results") if isinstance(content, dict) else None
                if not isinstance(results, list) or len(results) != len(calls):
                    continue
            else:
                calls, results = [intent.payload.get("call")], [receipt.result]
            for raw, result in zip(calls, results, strict=True):
                if (
                    isinstance(raw, dict)
                    and isinstance(result, dict)
                    and raw.get("name") in self.definitions
                ):
                    yield event, key, receipt, ModelToolCall.model_validate(raw), result

    async def _current(self, memory_id, body):
        from datetime import datetime, timezone

        record = await self.runtime.memory_read(
            self.context.bundle,
            MemoryRecordRequest(
                principal=self.principal,
                scopes=tuple(self.policy.scopes),
                memory_id=memory_id,
            ),
        )
        if (
            record is None
            or record.content != body
            or record.content_hash != content_hash(body)
            or record.version is None
        ):
            return None
        now = datetime.now(timezone.utc)
        start, end, expiry = (
            _time(value) for value in (record.valid_from, record.valid_to, record.expires_at)
        )
        if (start is not None and start > now) or any(
            value is not None and value <= now for value in (end, expiry)
        ):
            return None
        return record

    def _record_ref(self, record):
        # Content stays private; the exact observation has an auditable identity.
        return "memory-record:" + studio_mapping_digest(
            {
                "principal": self.principal.model_dump(mode="json"),
                "memory_id": record.memory_id,
                "version": record.version,
                "content_hash": record.content_hash,
            }
        )
