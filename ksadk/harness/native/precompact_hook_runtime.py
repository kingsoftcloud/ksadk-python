"""Receipt-controlled work on original history before a summary can be sent.

Callback data remains low-trust live context. Replan with it under the same
host budget; callbacks cannot replace summary instructions or acceptance.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from .context_runtime_contracts import CompactionRequest, _refs, source_context_digest
from .hook_runtime_contract import (
    HookInvocationRequest,
    HookInvocationResult,
    PreCompactSubject,
    _json_bytes,
    validate_hook_result,
)
from .state import Contract, RunPhase, RunSnapshot, move
from .transitions import finish, prepare_effect

if TYPE_CHECKING:
    from .ports import RunConfiguration


class PreCompactHookState(Contract):
    goal_revision: int = Field(strict=True, ge=0)
    source_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_cursor: int = Field(strict=True, ge=0)
    next_hook_index: int = Field(default=0, strict=True, ge=0)
    replan: bool = False
    feedback: tuple[dict[str, JsonValue], ...] = ()


def precompact_hook_state(snapshot: RunSnapshot) -> PreCompactHookState | None:
    if snapshot.precompact_hook_state is None:
        return None
    value = PreCompactHookState.model_validate_json(_json_bytes(snapshot.precompact_hook_state))
    return value if value.goal_revision == snapshot.goal_revision else None


def planned_precompact_state(
    snapshot: RunSnapshot, config: RunConfiguration, request: CompactionRequest,
):
    hooks = tuple(item for item in config.hooks if "PreCompact" in item.events)
    if not hooks:
        return None
    previous = precompact_hook_state(snapshot)
    if previous and previous.replan and previous.model_cursor == snapshot.model_calls:
        return previous.model_copy(update={
            "source_context_digest": request.source_context_digest, "replan": False,
        }).model_dump(mode="json")
    return PreCompactHookState(
        goal_revision=snapshot.goal_revision, source_context_digest=request.source_context_digest,
        model_cursor=snapshot.model_calls,
    ).model_dump(mode="json")


def precompact_feedback_messages(
    snapshot: RunSnapshot, messages: tuple[dict, ...],
) -> tuple[dict, ...]:
    current = precompact_hook_state(snapshot)
    if current is None:
        return messages
    seen = {item.get("hook_operation_key") for item in messages}
    return (*messages, *(item for item in current.feedback
                         if item["hook_operation_key"] not in seen))


def precompact_feedback_keys(snapshot: RunSnapshot) -> tuple[str, ...]:
    current = precompact_hook_state(snapshot)
    return tuple(item["hook_operation_key"] for item in current.feedback) if current else ()


def precompact_hook_request(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str,
) -> HookInvocationRequest:
    current = precompact_hook_state(snapshot)
    if (snapshot.phase != RunPhase.COMPACTING or current is None
            or snapshot.compaction_request is None):
        raise ValueError("PreCompact requires current compaction admission")
    compact = CompactionRequest.model_validate(snapshot.compaction_request)
    prefix = snapshot.messages[:compact.source_message_count]
    if (compact.source_context_digest != current.source_context_digest
            or source_context_digest(prefix) != compact.source_context_digest):
        raise ValueError("PreCompact source changed after planning")
    hooks = tuple(item for item in config.hooks if "PreCompact" in item.events)
    if current.next_hook_index >= len(hooks):
        raise ValueError("no pending PreCompact Hook")
    retained = set(compact.retained_message_refs)
    # After an earlier compaction old_messages describes the active subset,
    # while the source digest covers the immutable full transcript prefix.
    # Preserve original refs; positions in those two lists are not equivalent.
    original = dict(zip(_refs(prefix), prefix, strict=True))
    removed = tuple((item.message_id, original[item.message_id])
                    for item in compact.old_messages if item.message_id not in retained)
    projection = hooks[current.next_hook_index].compaction_source_projection
    if projection == "canonical_user":
        removed = tuple((ref, raw) for ref, raw in removed
                        if raw.get("role") == "user" and raw.get("kind") is None)
    return HookInvocationRequest(
        scope=snapshot.scope, run_id=snapshot.run_id, session_id=snapshot.session_id,
        turn_id=snapshot.turn_id, operation_key=operation_key,
        goal_revision=snapshot.goal_revision, goal=snapshot.goal,
        tool_call_count=snapshot.tool_calls, stop_continuation_count=0,
        event="PreCompact", definition=hooks[current.next_hook_index],
        compaction=PreCompactSubject(
            source_revision=compact.source_revision,
            source_context_digest=compact.source_context_digest,
            source_message_count=compact.source_message_count,
            message_refs=tuple(ref for ref, _ in removed),
            messages=tuple(raw for _, raw in removed),
            retained_message_refs=compact.retained_message_refs,
            source_projection=projection,
        ),
    )


def prepare_precompact_hook(snapshot: RunSnapshot, config: RunConfiguration):
    current = precompact_hook_state(snapshot)
    hooks = tuple(item for item in config.hooks if "PreCompact" in item.events)
    if current is None or current.next_hook_index >= len(hooks):
        return None
    key = f"{snapshot.run_id}:effect:{snapshot.revision + 1}"
    request = precompact_hook_request(snapshot, config, key)
    return prepare_effect(snapshot, "hook", {"request": request.model_dump(mode="json")})


def reduce_precompact_hook(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str, raw: dict,
) -> RunSnapshot:
    request = precompact_hook_request(snapshot, config, operation_key)
    result = validate_hook_result(request, HookInvocationResult.model_validate(raw))
    current = precompact_hook_state(snapshot)
    current = current.model_copy(update={"next_hook_index": current.next_hook_index + 1})
    updated = snapshot.model_copy(update={"pending_intent": None})
    if result.status == "uncertain" or result.reason == "binding_unavailable":
        return finish(updated, "unverified", "effect_outcome_unknown")
    if result.status == "failed":
        optional = result.reason in {"producer_failed", "producer_invalid", "producer_timeout",
                                     "input_budget_exceeded", "output_budget_exceeded"}
        if not optional or request.definition.failure_mode != "warn":
            return finish(updated, "blocked", "hook_" + result.reason)
    else:
        if result.output.action == "deny":
            return finish(updated, "blocked", "precompact_hook_denied")
        if result.output.additional_context:
            feedback = {
                "role": "user", "kind": "hook_context", "hook_operation_key": operation_key,
                "hook_goal_revision": snapshot.goal_revision,
                "content": json.dumps({
                    "event": "PreCompact", "hook_id": request.definition.hook_id,
                    "source_ref": request.definition.source_ref,
                    "binding_digest": request.definition.binding_digest,
                    "trust_level": "untrusted", "data": result.output.additional_context,
                }, ensure_ascii=False, sort_keys=True),
            }
            current = current.model_copy(update={"feedback": (*current.feedback, feedback)})
            updated = updated.model_copy(update={"messages": (*updated.messages, feedback)})
        if not request.definition.read_only:
            updated = updated.model_copy(update={"workspace_observation_cursor": None})
    hooks = tuple(item for item in config.hooks if "PreCompact" in item.events)
    if current.next_hook_index == len(hooks):
        current = current.model_copy(update={"replan": True})
        updated = move(updated, RunPhase.PREPARING_CONTEXT).model_copy(update={
            "compaction_request": None, "prepared_context": None,
        })
    return updated.model_copy(update={"precompact_hook_state": current.model_dump(mode="json")})
