"""Run-scoped local Memory ports for the independent Harness service."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from ksadk.context_engine.tokenizer import get_default_token_counter
from ksadk.harness.service_runtime import (
    ServiceContextSource,
    ServiceGoalVerifier,
    ServiceHook,
    ServiceHostTool,
)
from ksadk.memory.extraction import explicit_memory_target
from ksadk.memory.models import MemoryConflictError, MemoryRecord, MemorySearchResult
from ksadk.memory.policy import HARD_REJECT_LABELS, detect_sensitive_labels
from ksadk.plugins.builtins import LOCAL_MEMORY_PROVIDER_PLUGIN_ID
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.memory_provider import (
    MemoryFlushRequest,
    MemoryMutationRequest,
    MemoryPrincipal,
    MemoryRecallRequest,
    MemoryReceiptRequest,
    MemoryRecordRequest,
    MemoryUserObservation,
)
from ksadk.runtime import RunHandle
from ksadk.studio.contracts import ContextSpec, MemorySpec
from ksadk.studio.native_memory_acceptance import VERIFIER_REF, VERIFIER_VERSION
from ksadk.studio.native_memory_context import (
    admitted_memory_records,
    memory_record_expiry,
    selected_memory_binding,
)
from ksadk.studio.native_scope import native_scope_from_activation

if TYPE_CHECKING:
    from ksadk.plugins.providers.harness import HarnessEngineActivationContext


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _view(record: MemoryRecord) -> dict[str, Any]:
    return {
        "memory_id": record.memory_id, "content": record.content, "scope": record.scope,
        "version": record.version, "source_event_ids": record.source_event_ids,
        "source_session_id": record.source_session_id, "content_hash": record.content_hash,
        "updated_at": record.updated_at, "trust_level": "untrusted",
    }


def _error(code: str) -> dict[str, Any]:
    return {"status": "error", "content": {"code": code}}


def _verification_result(status, reason, summary, refs=()):
    return {
        "status": status, "reason": reason, "safeSummary": summary, "evidenceRefs": list(refs),
    }


class ServiceLocalMemory:
    def __init__(
        self, context: HarnessEngineActivationContext, policy: MemorySpec,
        context_policy: ContextSpec,
    ) -> None:
        self.context = context
        self.policy = policy
        self.bundle = context.bundle
        binding, ref = selected_memory_binding(context)
        if binding.plugin_id != LOCAL_MEMORY_PROVIDER_PLUGIN_ID:
            raise PluginHostError("harness_service_memory_unbound", "Local Memory owner is invalid")
        self.owner = binding.runtime
        self.provider_ref = ref
        self.principal = MemoryPrincipal(**native_scope_from_activation(context).model_dump())
        self.scopes = tuple(policy.scopes)
        self.rollout = context_policy.rollout.memory_write
        self.writes = policy.write.mode != "off" and self.rollout != "off"
        may_flush = self.writes and "user" in self.scopes
        self.flush_before = may_flush and policy.write.flush_before_compaction
        self.flush_after = may_flush and policy.write.flush_after_turn
        self.recall = bool(
            policy.recall.enabled and policy.recall.max_tokens
            and context_policy.contributors.memory_recall is not False
        )
        required = ["memory_read", "memory_search"]
        if self.recall:
            required.append("memory_recall")
        if self.writes:
            required.extend(["memory_mutate", "memory_receipt"])
        if self.flush_before or self.flush_after:
            required.append("memory_flush")
        if not all(inspect.iscoroutinefunction(getattr(self.owner, method, None))
                   for method in required):
            raise PluginHostError(
                "harness_service_memory_unbound", "Local Memory ports are invalid",
            )
        self.manifest = self.bundle.manifest.model_dump_json()
        self.profile = self.bundle.composition.profile.model_dump_json()
        self.lock = self.bundle.composition.plugin_lock.model_dump_json()
        self.binding = _digest([
            self.bundle.bundle_digest, context.profile_digest, context.plugin_lock_digest, ref,
            binding.slot, self.principal.model_dump(), policy.model_dump(), self.rollout,
            "local-memory-service/v1",
        ])

    def _available(self) -> None:
        available = self.context.activation_available
        if (
            available is None or not available()
            or self.bundle.manifest.model_dump_json() != self.manifest
            or self.bundle.composition.profile.model_dump_json() != self.profile
            or self.bundle.composition.plugin_lock.model_dump_json() != self.lock
        ):
            raise ValueError("Memory activation is no longer available")

    def _request(self, query: str, budget: int) -> MemoryRecallRequest:
        return MemoryRecallRequest(
            principal=self.principal, query=query, scopes=self.scopes,
            top_k=self.policy.recall.top_k, max_tokens=budget,
            min_score=self.policy.recall.min_score,
        )

    def _records(self, result: MemorySearchResult):
        return admitted_memory_records(
            result, principal=self.principal, scopes=self.scopes, top_k=self.policy.recall.top_k,
        )

    def _fit(self, views: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        counter = get_default_token_counter()
        admitted = []
        remaining = budget
        for view in views:
            cost = counter.count_text(json.dumps(view, ensure_ascii=False)) + 96
            if cost <= remaining:
                admitted.append(view)
                remaining -= cost
        return admitted

    async def _read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._available()
        request = MemoryRecordRequest(
            principal=self.principal, scopes=self.scopes, memory_id=arguments["memory_id"],
        )
        record = await self.owner.memory_read(self.bundle, request)
        self._available()
        candidates = MemorySearchResult("ok", [record] if record else [], None, "local-default", 0,
                                        "exact")
        views = [_view(item) for item in self._records(candidates)]
        return {"record": views[0]} if views else {"ok": False, "code": "memory_not_found_in_scope"}

    async def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._available()
        budget = self.policy.recall.max_tokens
        request = self._request(arguments["query"], budget)
        result = await self.owner.memory_search(self.bundle, request)
        self._available()
        candidates = [_view(item) for item in self._records(result)]
        records = self._fit(candidates, budget)
        return {"records": records, "truncated": len(records) < len(candidates)
                or result.truncated_by_budget, "trust_level": "untrusted"}

    async def _recall(self, handle: RunHandle, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._available()
        if handle.session_id != self.context.activation_key:
            raise ValueError("Memory recall session differs from activation")
        budget = min(params["remainingTokens"], self.policy.recall.max_tokens)
        if budget <= 0:
            return []
        result = await self.owner.memory_recall(self.bundle, self._request(params["goal"], budget))
        self._available()
        candidates = []
        for record in self._records(result):
            item = {"projection": "data", "fragment": {"kind": "recalled_memory", **_view(record)}}
            expiry = memory_record_expiry(record)
            if expiry is not None:
                item["expiresAt"] = int(expiry.timestamp() * 1000)
            candidates.append(item)
        return self._fit(candidates, budget)

    async def _mutate(self, handle: RunHandle, params: dict[str, Any]) -> dict[str, Any]:
        self._available()
        if handle.session_id != self.context.activation_key:
            return _error("memory_session_unbound")
        arguments, effect = params["arguments"], params["effect"]
        observations = self._observations(handle, effect["observations"])
        explicit = explicit_memory_target(effect["goal"])
        authorized = (
            explicit is not None and explicit[1] == arguments.get("content")
            and {"add": "remember", "update": "correct"}.get(explicit[0]) == arguments["action"]
            and any(
                observation.content == effect["goal"]
                and observation.source_event_id.startswith(f"native-message://{handle.run_id}#")
                for observation in observations
            )
        )
        if not authorized and self.rollout != "shadow" and not effect["approved"]:
            if effect["canRequestApproval"]:
                return {"status": "approval_required"}
            return _error("memory_approval_required")
        authority = "approved_call" if effect["approved"] else "candidate"
        if authorized:
            authority = "explicit_user"
        receipt_request = self._receipt_request(
            handle, effect["operationKey"], params["callId"], arguments, authority,
        )
        try:
            mutation = MemoryMutationRequest(
                **receipt_request.model_dump(), **arguments,
                authority=authority,
                source_session_id=handle.session_id, source_event_id=effect["sourceEventId"],
                source_goal_revision=effect["goalRevision"],
                source_goal_digest=_digest({"goal": effect["goal"]}),
                source_user_observations=observations if arguments.get("scope") == "user" else (),
            )
        except ValueError:
            return _error("invalid_memory_action_arguments")
        try:
            persisted = await self.owner.memory_receipt(self.bundle, receipt_request)
            if persisted is not None:
                self._available()
                return {"status": "ok", "content": persisted}
            result = await self.owner.memory_mutate(self.bundle, mutation)
        except MemoryConflictError:
            return _error("memory_version_or_identity_conflict")
        except PluginHostError as error:
            if error.code == "memory_write_disabled":
                return _error("memory_write_disabled")
            raise
        self._available()
        if result.get("committed") is True:
            persisted = await self.owner.memory_receipt(self.bundle, receipt_request)
            if persisted is None or persisted.get("committed") is not True:
                return {"status": "uncertain"}
            self._available()
            return {"status": "ok", "content": persisted}
        status = "ok" if result.get("decision") == "shadow" else "error"
        return {"status": status, "content": result}

    def _receipt_request(self, handle, operation_key, call_id, arguments, authority):
        return MemoryReceiptRequest(
            principal=self.principal,
            operation_key=_digest([self.principal.model_dump(), handle.run_id, operation_key]),
            request_digest=_digest([self.binding, handle.run_id, call_id, arguments, authority]),
        )

    async def _current(self, memory_id, body):
        if not isinstance(memory_id, str):
            return None
        result = await self._read({"memory_id": memory_id})
        record = result.get("record")
        if not record or record["content"] != body:
            return None
        return "memory-record:" + _digest([
            self.principal.model_dump(), memory_id, record["version"], record["content_hash"],
        ])

    async def _confirm_write(self, handle, evidence, body):
        content = evidence["content"]
        if (
            evidence["status"] != "ok" or not isinstance(content, dict)
            or content.get("committed") is not True
        ):
            return None
        call = evidence["call"]
        for authority in ("explicit_user", "approved_call"):
            request = self._receipt_request(
                handle, evidence["operationKey"], call["callId"], call["arguments"], authority,
            )
            try:
                receipt = await self.owner.memory_receipt(self.bundle, request)
            except MemoryConflictError:
                continue
            self._available()
            if receipt == content:
                return await self._current(content.get("memory_id"), body)
        return None

    async def verify_goal(self, handle, params):
        self._available()
        if handle.session_id != self.context.activation_key:
            raise ValueError("Memory verification session differs from activation")
        operation, body = explicit_memory_target(params["goal"])
        evidence = params["evidence"]
        if operation == "add":
            recalled = [row["item"] for row in evidence["context"]]
            for row in evidence["calls"]:
                name, content = row["call"]["name"], row["content"]
                if row["status"] != "ok" or not isinstance(content, dict):
                    continue
                if name == "read_memory" and isinstance(content.get("record"), dict):
                    recalled.append(content["record"])
                if name == "search_memory" and isinstance(content.get("records"), list):
                    recalled.extend(content["records"])
            for item in recalled:
                if item.get("content") == body:
                    if reference := await self._current(item.get("memory_id"), body):
                        return _verification_result(
                            "passed", "current_durable_fact",
                            "The exact requested fact is present in scoped durable Memory.",
                            [reference],
                        )
        if not self.writes or self.rollout != "enabled":
            return _verification_result(
                "blocked", "memory_write_not_committing",
                "This Build does not commit Memory writes.",
            )
        if len(body) > 8192 or HARD_REJECT_LABELS.intersection(detect_sensitive_labels(body, [])):
            return _verification_result(
                "blocked", "memory_fact_rejected",
                "This fact cannot be written under Memory policy.",
            )
        action = {"add": "remember", "update": "correct"}[operation]
        for row in evidence["calls"]:
            call, content = row["call"], row["content"]
            if (
                call["name"] != "manage_memory" or call["arguments"].get("action") != action
                or call["arguments"].get("content") != body
            ):
                continue
            if isinstance(content, dict) and content.get("code") == "policy_denied":
                return _verification_result(
                    "blocked", "memory_approval_denied", "The requested Memory write was denied.",
                )
            if reference := await self._confirm_write(handle, row, body):
                return _verification_result(
                    "passed", "current_committed_fact",
                    "The owner receipt matches the exact current scoped durable record.",
                    [reference, "engine-event:" + _digest(row["eventId"])],
                )
        check = _digest([self.binding, handle.run_id, params["goalRevision"], body])
        return _verification_result(
            "failed", "durable_fact_missing",
            "The requested durable fact is missing. Use manage_memory and inspect its receipt. "
            "For a correction, read the exact record and use its current version.",
            ["memory-check:" + check],
        )

    def _observations(
        self, handle: RunHandle, rows: list[dict[str, Any]], *, current_only: bool = False,
    ) -> tuple[MemoryUserObservation, ...]:
        if handle.session_id != self.context.activation_key:
            raise ValueError("Memory observation session differs from activation")
        observations = []
        for row in rows:
            run_id, event_id = row["sourceRunId"], row["sourceEventId"]
            index = event_id.removeprefix(f"native-message://{run_id}#message=")
            if not index.isascii() or not index.isdecimal():
                raise ValueError("Memory observation source identity is invalid")
            if current_only and run_id != handle.run_id:
                raise ValueError("Completed-Turn source differs from original Run")
            observations.append(MemoryUserObservation(
                source_event_id=event_id, source_session_id=handle.session_id,
                source_digest=row["sourceDigest"], content=row["content"],
            ))
        return tuple(observations)

    async def _flush(self, request: MemoryFlushRequest) -> dict[str, Any]:
        self._available()
        result = await self.owner.memory_flush(self.bundle, request)
        self._available()
        return result

    async def complete(self, handle: RunHandle, params: dict[str, Any]) -> None:
        if not self.flush_after or "manage_memory" in params["effectTools"]:
            return
        observations = self._observations(handle, params["observations"], current_only=True)
        if observations:
            await self._flush(MemoryFlushRequest(
                principal=self.principal, trigger="completed_turn", observations=observations,
            ))

    async def _pre_compact(self, handle: RunHandle, params: dict[str, Any]) -> dict[str, Any]:
        if params["event"] != "PreCompact":
            raise ValueError("Memory flush requires a PreCompact effect")
        observations = self._observations(handle, params["observations"])
        result = await self._flush(MemoryFlushRequest(
            principal=self.principal, trigger="pre_compaction", observations=observations,
        ))
        feedback = {
            "memory_candidate_flush": result["status"], "proposed": result["proposed"],
            "decisions": dict(Counter(item["decision"] for item in result["results"])),
            "current_fact_verification": False, "trust_level": "untrusted",
        }
        return {"status": "ok", "reason": None, "output": {
            "action": "continue", "updatedArguments": None,
            "additionalContext": [json.dumps(feedback, ensure_ascii=False)],
        }}

    def hooks(self) -> tuple[ServiceHook, ...]:
        if not self.flush_before:
            return ()
        return (ServiceHook({
            "hookId": "ksadk.memory.precompact", "sourceRef": self.provider_ref,
            "bindingDigest": self.binding,
            "executionPolicyDigest": _digest(["ksadk-service-memory-precompact/v1", 3000]),
            "readOnly": self.rollout == "shadow", "events": ["PreCompact"],
            "timeoutMs": 3000, "maxInputBytes": 262144, "maxOutputBytes": 16384,
            "failureMode": "warn", "compactionSourceProjection": "canonical_user",
        }, self._pre_compact),)

    def context_sources(self) -> tuple[ServiceContextSource, ...]:
        if not self.recall:
            return ()
        return (ServiceContextSource({
            "sourceId": "ksadk-memory-recall", "bindingDigest": self.binding,
            "maxTokens": self.policy.recall.max_tokens, "maxBytes": 131072,
            "timeoutMs": 3000, "failureMode": "skip", "allowInstructions": False,
        }, self._recall),)

    def tools(self) -> list[ServiceHostTool]:
        def schema(properties, required):
            return {"type": "object", "additionalProperties": False,
                    "properties": properties, "required": required}

        identity = {"type": "string", "minLength": 1, "maxLength": 256}
        tools = [ServiceHostTool(
            "read_memory", "Read an exact scoped memory record and its version.",
            schema({"memory_id": identity}, ["memory_id"]), self._read, read_only=True,
        )]
        if self.policy.recall.max_tokens:
            tools.append(ServiceHostTool(
                "search_memory", "Search this Agent's granted scopes for relevant Memory.",
                schema({"query": {"type": "string", "minLength": 1, "maxLength": 4096}}, ["query"]),
                self._search, read_only=True,
            ))
        if self.writes:
            tools.append(ServiceHostTool(
                "manage_memory", "Remember, correct, or forget a scoped fact. Correct and forget "
                "require the exact ID and version. Only a committed receipt proves a write.",
                schema({
                    "action": {"type": "string", "enum": ["remember", "correct", "forget"]},
                    "scope": {"type": "string", "enum": list(self.scopes)},
                    "content": {"type": "string", "maxLength": 8192},
                    "memory_id": identity, "expected_version": {"type": "integer", "minimum": 1},
                }, ["action", "scope"]), None, effect=self._mutate,
            ))
        return tools


def service_local_memory(context: HarnessEngineActivationContext) -> ServiceLocalMemory | None:
    policy = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    if not policy.enabled or not policy.scopes or policy.provider_ref != "local-default":
        return None
    context_policy = ContextSpec.model_validate(
        context.bundle.resolved_agent_spec.get("context") or {},
    )
    return ServiceLocalMemory(context, policy, context_policy)


def service_memory_verifier(
    context: HarnessEngineActivationContext, memory: ServiceLocalMemory | None,
) -> ServiceGoalVerifier | None:
    policy = MemorySpec.model_validate(context.bundle.resolved_agent_spec.get("memory") or {})
    if policy.provider_ref != "local-default":
        return None

    async def disabled(_handle, _params):
        return _verification_result(
            "blocked", "memory_not_enabled_in_build", "This Build has no active local Memory.",
        )

    return ServiceGoalVerifier({
        "verifierRef": VERIFIER_REF, "verifierVersion": VERIFIER_VERSION,
        "bindingDigest": memory.binding if memory else _digest([
            context.bundle.bundle_digest, context.plugin_lock_digest, "disabled-local-memory",
        ]),
        "evidenceTools": [tool.name for tool in memory.tools()] if memory else [],
        "contextSourceId": "ksadk-memory-recall" if memory and memory.recall else None,
    }, lambda goal: explicit_memory_target(goal) is not None,
        memory.verify_goal if memory else disabled)
