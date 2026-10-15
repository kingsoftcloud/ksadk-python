"""Durable Stop proposals before final verification, without completion grants."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from .completion_text import candidate_model_message
from .hook_runtime_contract import (
    HookInvocationRequest,
    HookInvocationResult,
    _json_bytes,
    validate_hook_result,
)
from .state import Contract, RunPhase, RunSnapshot, move
from .transitions import finish, prepare_effect

if TYPE_CHECKING:
    from .ports import RunConfiguration


class StopHookState(Contract):
    goal_revision: int = Field(strict=True, ge=0)
    candidate_id: str | None = None
    next_hook_index: int = Field(default=0, strict=True, ge=0)
    continuation_count: int = Field(default=0, strict=True, ge=0)
    feedback: tuple[dict[str, JsonValue], ...] = ()


def stop_hook_state(snapshot: RunSnapshot) -> StopHookState:
    if snapshot.stop_hook_state is not None:
        state = StopHookState.model_validate_json(_json_bytes(snapshot.stop_hook_state))
        if state.goal_revision == snapshot.goal_revision:
            return state
    return StopHookState(goal_revision=snapshot.goal_revision)


def hook_feedback_messages(snapshot: RunSnapshot, messages: tuple[dict, ...]) -> tuple[dict, ...]:
    """Reproject pending feedback before context budgeting, even after pruning."""
    state = stop_hook_state(snapshot)
    seen = {message.get("hook_operation_key") for message in messages}
    return (*messages, *(item for item in state.feedback
                         if item["hook_operation_key"] not in seen))


def stop_hook_request(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str,
) -> HookInvocationRequest:
    candidate = snapshot.candidate or {}
    if (snapshot.phase != RunPhase.VERIFYING or candidate.get("purpose") == "checkpoint"
            or not candidate.get("candidate_id") or "final_text" not in candidate):
        raise ValueError("Stop requires a final completion candidate")
    state = stop_hook_state(snapshot)
    index = state.next_hook_index if state.candidate_id == candidate["candidate_id"] else 0
    hooks = tuple(hook for hook in config.hooks if "Stop" in hook.events)
    if index >= len(hooks):
        raise ValueError("no pending Stop Hook")
    return HookInvocationRequest(
        scope=snapshot.scope, run_id=snapshot.run_id, session_id=snapshot.session_id,
        turn_id=snapshot.turn_id, operation_key=operation_key,
        goal_revision=snapshot.goal_revision, goal=snapshot.goal,
        tool_call_count=snapshot.tool_calls, stop_continuation_count=state.continuation_count,
        event="Stop", definition=hooks[index],
        candidate_id=candidate["candidate_id"], candidate_text=candidate["final_text"],
    )


def prepare_stop_hook(snapshot: RunSnapshot, config: RunConfiguration):
    candidate = snapshot.candidate or {}
    if (snapshot.phase != RunPhase.VERIFYING or snapshot.browser_reobservation_required
            or candidate.get("purpose") == "checkpoint"):
        return None
    hooks = tuple(hook for hook in config.hooks if "Stop" in hook.events)
    if not hooks:
        return None
    state = stop_hook_state(snapshot)
    index = state.next_hook_index if state.candidate_id == candidate.get("candidate_id") else 0
    if index >= len(hooks):
        return None
    key = f"{snapshot.run_id}:effect:{snapshot.revision + 1}"
    request = stop_hook_request(snapshot, config, key)
    return prepare_effect(snapshot, "hook", {"request": request.model_dump(mode="json")})


def reduce_stop_hook(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str, result: dict,
) -> RunSnapshot:
    request = stop_hook_request(snapshot, config, operation_key)
    result = validate_hook_result(request, HookInvocationResult.model_validate(result))
    state = stop_hook_state(snapshot)
    index = state.next_hook_index if state.candidate_id == request.candidate_id else 0
    state = state.model_copy(update={"candidate_id": request.candidate_id,
                                    "next_hook_index": index + 1})
    updated = snapshot.model_copy(update={"pending_intent": None})
    if result.status == "uncertain" or result.reason == "binding_unavailable":
        return finish(updated.model_copy(update={"candidate": None}),
                      "unverified", "effect_outcome_unknown")
    if result.status == "failed":
        optional_failure = result.reason in {
            "producer_failed", "producer_invalid", "producer_timeout",
            "input_budget_exceeded", "output_budget_exceeded",
        }
        if not optional_failure or request.definition.failure_mode != "warn":
            return finish(updated.model_copy(update={"candidate": None}),
                          "blocked", "hook_" + result.reason)
    else:
        output = result.output
        if output.action == "deny":
            return finish(updated.model_copy(update={"candidate": None}),
                          "blocked", "stop_hook_denied")
        if output.additional_context or output.action == "repair":
            message = {
                "role": "user", "kind": "hook_context", "hook_operation_key": operation_key,
                "hook_goal_revision": snapshot.goal_revision,
                "content": json.dumps({
                    "event": "Stop", "hook_id": request.definition.hook_id,
                    "source_ref": request.definition.source_ref,
                    "binding_digest": request.definition.binding_digest,
                    "action": output.action,
                    "candidate_status": "rejected" if output.action == "repair" else "unverified",
                    "data": output.additional_context,
                }, ensure_ascii=False, sort_keys=True),
            }
            state = state.model_copy(update={"feedback": (*state.feedback, message)})
            updated = updated.model_copy(update={"messages": (*updated.messages, message)})
        if not request.definition.read_only:
            updated = updated.model_copy(update={"workspace_observation_cursor": None})
        if output.action == "repair":
            limit = config.hook_stop_continuation_limit
            if limit is None or state.continuation_count >= limit:
                return finish(updated.model_copy(update={"candidate": None}),
                              "blocked", "stop_hook_continuation_limit")
            state = state.model_copy(update={
                "candidate_id": None, "next_hook_index": 0,
                "continuation_count": state.continuation_count + 1,
            })
            updated = move(updated, RunPhase.PREPARING_CONTEXT).model_copy(update={
                "messages": (
                    *snapshot.messages,
                    candidate_model_message(snapshot),
                    *updated.messages[len(snapshot.messages):],
                ),
                "candidate": None, "candidate_replay": None, "prepared_context": None,
                "verification": None, "verification_tool_calls": None, "delivery_review": None,
                # A Hook repair asks for a new answer, not the rejected
                # candidate's suffix. Its separate Stop cap remains charged.
                "continuations": 0,
            })
    return updated.model_copy(update={"stop_hook_state": state.model_dump(mode="json")})
