"""Pure durable tool Hook admission; callbacks never own tools or approval."""

from __future__ import annotations

import json

from pydantic import Field, JsonValue

from .hook_runtime_contract import (
    HookInvocationRequest,
    HookInvocationResult,
    _json_bytes,
    validate_hook_result,
)
from .model import ModelToolCall
from .ports import RunConfiguration, ToolResult
from .precompact_hook_runtime import precompact_hook_state
from .state import Contract, RunPhase, RunSnapshot
from .stop_hook_runtime import stop_hook_state
from .tool_budget import refused_pending_tools, tools_exhausted
from .tool_input_validation import ToolInputValidator
from .tool_scheduling import scheduled_tool_calls
from .transitions import finish, prepare_effect


class ToolHookCall(Contract):
    original: ModelToolCall
    effective: ModelToolCall
    next_pre_index: int = Field(default=0, strict=True, ge=0)
    denied: bool = False


class PostHookPending(Contract):
    call: ModelToolCall
    result: ToolResult
    next_index: int = Field(default=0, strict=True, ge=0)


class ToolHookState(Contract):
    goal_revision: int = Field(strict=True, ge=0)
    calls: tuple[ToolHookCall, ...] = ()
    post: tuple[PostHookPending, ...] = ()
    feedback: tuple[dict[str, JsonValue], ...] = ()


def tool_hook_state(snapshot: RunSnapshot) -> ToolHookState:
    if snapshot.tool_hook_state is not None:
        state = ToolHookState.model_validate_json(_json_bytes(snapshot.tool_hook_state))
        if state.goal_revision == snapshot.goal_revision:
            return state
    return ToolHookState(goal_revision=snapshot.goal_revision)


def _call_state(state: ToolHookState, call: ModelToolCall) -> ToolHookCall:
    record = next((item for item in state.calls if item.original.call_id == call.call_id), None)
    if record is not None and _json_bytes(record.effective.model_dump(mode="json")) != _json_bytes(
        call.model_dump(mode="json")
    ):
        raise ValueError("effective Hook call differs from pending tool")
    return record or ToolHookCall(original=call, effective=call)


def _valid_input(config: RunConfiguration, call: ModelToolCall) -> bool:
    definition = next((tool for tool in config.tools if tool.name == call.name), None)
    allowed = config.contract.allowed_tool_names
    if (
        definition is None
        or call.wire_arguments_invalid
        or (allowed is not None and call.name not in allowed)
    ):
        return False
    try:
        return ToolInputValidator(definition.parameters).rejection(call.arguments) is None
    except ValueError:
        return False


def tool_hook_request(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str
) -> HookInvocationRequest | None:
    if snapshot.phase != RunPhase.EXECUTING_TOOLS:
        return None
    state = tool_hook_state(snapshot)
    if state.post:
        pending = state.post[0]
        hooks = tuple(h for h in config.hooks if h.matches("PostToolUse", pending.call.name))
        if pending.next_index >= len(hooks):
            raise ValueError("invalid pending PostToolUse cursor")
        call, result, event, hook = (
            pending.call,
            pending.result.model_dump(mode="json"),
            "PostToolUse",
            hooks[pending.next_index],
        )
    else:
        if tools_exhausted(snapshot, config):
            return None
        selected = None
        for raw in scheduled_tool_calls(snapshot, config):
            current = ModelToolCall.model_validate(raw)
            record = _call_state(state, current)
            if record.denied or not _valid_input(config, current):
                continue  # Invalid original/proposed input never invokes another Hook.
            hooks = tuple(h for h in config.hooks if h.matches("PreToolUse", current.name))
            if record.next_pre_index < len(hooks):
                selected = (current, hooks[record.next_pre_index])
                break
        if selected is None:
            return None
        call, hook = selected
        result, event = None, "PreToolUse"
    return HookInvocationRequest(
        scope=snapshot.scope,
        run_id=snapshot.run_id,
        session_id=snapshot.session_id,
        turn_id=snapshot.turn_id,
        operation_key=operation_key,
        goal=snapshot.goal,
        goal_revision=snapshot.goal_revision,
        tool_call_count=snapshot.tool_calls,
        stop_continuation_count=stop_hook_state(snapshot).continuation_count,
        event=event,
        definition=hook,
        call=call,
        tool_result=result,
    )


def prepare_tool_hook(snapshot: RunSnapshot, config: RunConfiguration):
    if not any(event != "Stop" for hook in config.hooks for event in hook.events):
        return None
    request = tool_hook_request(
        snapshot, config, f"{snapshot.run_id}:effect:{snapshot.revision + 1}"
    )
    return (
        prepare_effect(snapshot, "hook", {"request": request.model_dump(mode="json")})
        if request is not None
        else None
    )


def close_tool_hook_run(snapshot: RunSnapshot, outcome: str, reason: str) -> RunSnapshot:
    # These pending tool calls have not been dispatched. Unknown Hook effects
    # are independent facts and do not authorize replay of either operation.
    closed = snapshot.model_copy(
        update={
            "candidate": None,
            "messages": (
                *snapshot.messages,
                *refused_pending_tools(snapshot, reason=reason),
            ),
        }
    )
    return finish(closed, outcome, reason)


def reduce_tool_hook(
    snapshot: RunSnapshot, config: RunConfiguration, operation_key: str, raw_result: dict
) -> RunSnapshot:
    request = tool_hook_request(snapshot, config, operation_key)
    if request is None:
        raise ValueError("tool Hook has no admitted subject")
    result = validate_hook_result(request, HookInvocationResult.model_validate(raw_result))
    state = tool_hook_state(snapshot)
    updated = snapshot.model_copy(update={"pending_intent": None})
    if result.status == "uncertain" or result.reason == "binding_unavailable":
        return close_tool_hook_run(updated, "unverified", "effect_outcome_unknown")
    if result.status == "failed":
        optional = result.reason in {
            "producer_failed",
            "producer_invalid",
            "producer_timeout",
            "input_budget_exceeded",
            "output_budget_exceeded",
        }
        if not optional or request.definition.failure_mode != "warn":
            return close_tool_hook_run(updated, "blocked", "hook_" + result.reason)
    output = result.output
    if request.event == "PreToolUse":
        record = _call_state(state, request.call)
        record = record.model_copy(update={"next_pre_index": record.next_pre_index + 1})
        if output is not None:
            if output.action == "deny":
                record = record.model_copy(update={"denied": True})
            elif output.updated_arguments is not None:
                call = ModelToolCall.model_validate(
                    {
                        **record.effective.model_dump(mode="json"),
                        "arguments": output.updated_arguments,
                    }
                )
                record = record.model_copy(update={"effective": call})
                # Keep the provider's assistant call untouched. Effective
                # arguments become the next actual tool request and approval.
                updated = updated.model_copy(
                    update={
                        "pending_tools": tuple(
                            call.model_dump(mode="json")
                            if item["call_id"] == call.call_id
                            else item
                            for item in updated.pending_tools
                        ),
                        "interaction": None,
                        "interaction_host_id": None,
                    }
                )
        records = tuple(
            item for item in state.calls if item.original.call_id != request.call.call_id
        )
        state = state.model_copy(update={"calls": (*records, record)})
    else:
        pending = state.post[0]
        hooks = tuple(h for h in config.hooks if h.matches("PostToolUse", pending.call.name))
        state = state.model_copy(
            update={
                "post": (
                    (
                        pending.model_copy(update={"next_index": pending.next_index + 1}),
                        *state.post[1:],
                    )
                    if pending.next_index + 1 < len(hooks)
                    else state.post[1:]
                )
            }
        )
    if output is not None:
        if not request.definition.read_only:
            updated = updated.model_copy(update={"workspace_observation_cursor": None})
        if output.additional_context or output.updated_arguments is not None:
            data = {
                "event": request.event,
                "hook_id": request.definition.hook_id,
                "source_ref": request.definition.source_ref,
                "binding_digest": request.definition.binding_digest,
                "call_id": request.call.call_id,
                "data": output.additional_context,
            }
            if output.updated_arguments is not None:
                data["effective_arguments"] = output.updated_arguments
            message = {
                "role": "user",
                "kind": "hook_context",
                "hook_operation_key": operation_key,
                "hook_goal_revision": snapshot.goal_revision,
                "content": json.dumps(data, ensure_ascii=False, sort_keys=True),
            }
            state = state.model_copy(update={"feedback": (*state.feedback, message)})
    updated = updated.model_copy(update={"tool_hook_state": state.model_dump(mode="json")})
    if output is not None and output.action == "deny" and request.event == "PostToolUse":
        return close_tool_hook_run(updated, "blocked", "post_tool_hook_denied")
    return updated


def tool_hook_denial(snapshot: RunSnapshot, call: ModelToolCall) -> ToolResult | None:
    record = _call_state(tool_hook_state(snapshot), call)
    return (
        ToolResult(status="error", content={"code": "pre_tool_hook_denied", "executed": False})
        if record.denied
        else None
    )


def stage_post_hooks(
    previous: RunSnapshot,
    updated: RunSnapshot,
    config: RunConfiguration,
    calls: tuple[dict, ...],
    results: tuple[ToolResult, ...],
) -> RunSnapshot:
    if (
        not any("PostToolUse" in hook.events for hook in config.hooks)
        or updated.phase != RunPhase.EXECUTING_TOOLS
        or len(calls) != len(results)
    ):
        return updated
    state, pending = tool_hook_state(updated), []
    for raw, result in zip(calls, results, strict=True):
        call = ModelToolCall.model_validate(raw)
        record = _call_state(tool_hook_state(previous), call)
        if result.status not in {"ok", "error"} or record.denied or not _valid_input(config, call):
            continue
        if any(h.matches("PostToolUse", call.name) for h in config.hooks):
            pending.append(PostHookPending(call=call, result=result))
    if not pending:
        return updated
    state = state.model_copy(update={"post": (*state.post, *pending)})
    return updated.model_copy(update={"tool_hook_state": state.model_dump(mode="json")})


def pending_hook_operation_keys(snapshot: RunSnapshot) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item["hook_operation_key"]
            for item in (
                *stop_hook_state(snapshot).feedback,
                *tool_hook_state(snapshot).feedback,
            )
        )
    )


def consume_hook_feedback(snapshot: RunSnapshot) -> RunSnapshot:
    """A sealed task response acknowledges input delivery, never task success."""
    values = {}
    if snapshot.tool_hook_state is not None:
        values["tool_hook_state"] = (
            tool_hook_state(snapshot).model_copy(update={"feedback": ()}).model_dump(mode="json")
        )
    if snapshot.stop_hook_state is not None:
        values["stop_hook_state"] = (
            stop_hook_state(snapshot).model_copy(update={"feedback": ()}).model_dump(mode="json")
        )
    compact = precompact_hook_state(snapshot)
    if compact is not None and compact.feedback:
        values["precompact_hook_state"] = (
            compact.model_copy(update={"feedback": ()}).model_dump(mode="json")
        )
    return snapshot.model_copy(update=values)


def hook_execution_metadata(snapshot: RunSnapshot) -> dict:
    call = ModelToolCall.model_validate(snapshot.pending_tools[0])
    record = _call_state(tool_hook_state(snapshot), call)
    if _json_bytes(record.original.arguments) == _json_bytes(call.arguments):
        return {}
    return {"hook_effective_arguments": call.arguments}


def tool_hook_feedback_messages(
    snapshot: RunSnapshot, messages: tuple[dict, ...]
) -> tuple[dict, ...]:
    # Never insert a user frame between an assistant tool batch and its results.
    if snapshot.pending_tools or tool_hook_state(snapshot).post:
        return messages
    seen = {message.get("hook_operation_key") for message in messages}
    return (
        *messages,
        *(
            item
            for item in tool_hook_state(snapshot).feedback
            if item["hook_operation_key"] not in seen
        ),
    )
