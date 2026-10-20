"""Pure admission of one complete ArtifactRead through the real planner.

A page may be fully quoted in one bounded summary instead of retained beside
it. These detached projections never become receipts or dispatch any effects.
"""

from __future__ import annotations

import json

from .artifact_partial_read_capacity import _valid_arguments
from .artifact_read_capacity import _declared_group
from .artifact_read_contract import ARTIFACT_READ_DEFINITION
from .call_budget import call_room, remaining_calls
from .context_artifact_page import artifact_page_observation
from .context_fresh_tools import FRESH_TOOL_OBSERVATION_POLICY, fresh_tool_group_refs
from .context_partition import source_manifest
from .context_runtime import CompactionController, CompactionHistory
from .delivery_review import review_pending
from .effective_configuration import effective_run_configuration
from .machine import next_transition, reduce_tool
from .ports import ToolResult, digest
from .state import RunPhase
from .tool_budget import (
    finalization_only,
    model_instructions,
    model_tool_surface,
    model_turn_control,
)
from .transitions import commit_transition

SINGLE_ARTIFACT_CAPACITY_POLICY = "single-read-complete-planner-admission/v1"


def single_artifact_group_admission(snapshot, configuration, driver):
    """Quote one original read, without guessing other results or commands."""
    if (
        snapshot.configuration_digest != configuration.digest
        or snapshot.profile_digest != digest(configuration.profile)
        or snapshot.context_binding != driver.binding
    ):
        raise ValueError("source_read_configuration_mismatch")
    planner = driver.planner
    if (
        planner is None
        or snapshot.phase != RunPhase.EXECUTING_TOOLS
        or snapshot.force_compaction
        or review_pending(snapshot)
        or len(snapshot.pending_tools) != 1
        or snapshot.pending_intent != f"{snapshot.run_id}:effect:{snapshot.revision}"
        or snapshot.revision < 1
        or snapshot.event_seq < 1
    ):
        return None
    group = _declared_group(snapshot)
    if group is None or len(group[0]["tool_calls"]) != 1 or group[1]:
        return None
    call = snapshot.pending_tools[0]
    if not _valid_arguments(call["arguments"]):
        return None
    config = effective_run_configuration(configuration, snapshot)
    installed = [t for t in config.tools if t.name == ARTIFACT_READ_DEFINITION.name]
    remaining_models = remaining_calls(config.limits.max_model_calls,
                                       snapshot.model_calls + snapshot.descendant_model_calls)
    if (
        len(installed) != 1
        or installed[0].model_copy(update={"feedback_profile_digest": None})
        != ARTIFACT_READ_DEFINITION
        or not call_room(config.limits.max_tool_calls,
                         snapshot.tool_calls + snapshot.descendant_tool_calls)
        or remaining_models is not None and remaining_models < 1
    ):
        return None
    current = snapshot.model_copy(deep=True)
    prepared = next_transition(current.model_copy(update={
        "revision": current.revision - 1,
        "event_seq": current.event_seq - 1,
        "pending_intent": None,
    }), config)
    if (
        prepared is None or len(prepared.intents) != 1
        or prepared.intents[0].kind != "tool"
        or prepared.intents[0].operation_key != current.pending_intent
        or prepared.intents[0].payload != {"call": call}
        or prepared.snapshot != current
    ):
        return None
    ref = next((r for r in current.artifact_refs if r["uri"] == call["arguments"]["uri"]), None)
    if ref is None:
        return None

    def admit(result):
        if (
            not isinstance(result, ToolResult) or result.status != "ok"
            or result != ToolResult(status="ok", content=result.content)
            or not isinstance(result.content, dict)
            or any(result.content.get(k) != v for k, v in ref.items())
            or not (result.content.get("content") or result.content.get("complete") is True)
        ):
            return False
        message = {"role": "tool", "tool_call_id": call["call_id"],
                   "content": json.dumps({"status": "ok", "content": result.content},
                                         ensure_ascii=False)}
        if artifact_page_observation(message, arguments=call["arguments"]) is None:
            return False
        received = commit_transition(
            current, reduce_tool(current, result, definition=installed[0]), "effect.received",
        )
        completed = next_transition(received.snapshot, config)
        if (
            completed is None or completed.intents
            or completed.snapshot.phase != RunPhase.PREPARING_CONTEXT
            or completed.snapshot.pending_tools
            or completed.snapshot.messages[:len(current.messages)] != current.messages
            or len(completed.snapshot.messages) != len(current.messages) + 1
            or completed.snapshot.tool_calls != current.tool_calls + 1
            or completed.snapshot.model_calls != current.model_calls
            or completed.snapshot.compaction_history != current.compaction_history
        ):
            return False
        projected = completed.snapshot
        continuity = driver.continuity(projected, config)
        plan = planner.plan(
            source_revision=projected.revision, model_turn=projected.model_calls,
            profile=config.profile, instructions=model_instructions(projected, config),
            tools=model_tool_surface(projected, config), context_tools=config.tools,
            messages=projected.messages, continuity=continuity, contract=config.contract,
            history=CompactionHistory.model_validate(projected.compaction_history),
            force_compaction=False, turn_control=model_turn_control(projected, config),
            allow_compaction=not finalization_only(projected, config),
        )
        if plan.action == "ready":
            return bool(plan.prepared and plan.prepared.counted_input_tokens
                        + config.profile.max_output_tokens <= config.profile.context_window)
        if plan.action != "compact" or (remaining_models is not None and remaining_models < 2):
            return False
        request = plan.request
        if (
            request is None or request.remaining_history_entries
            or request.summary_output_tokens != config.profile.max_output_tokens
            or request.fresh_tool_observation_policy != FRESH_TOOL_OBSERVATION_POLICY
            or frozenset(request.fresh_tool_message_refs)
            != fresh_tool_group_refs(projected.messages)
            or request.summary_partition is None or request.summary_partition.pending is None
            or request.summary_partition.pending_source_parts
        ):
            return False
        # Require complete coverage in this request, not a promise that later
        # unknown summary chunks will fit. The real seal still checks output.
        CompactionController(planner)._validate_source(
            request, projected.revision, continuity, projected.messages,
        )
        manifest, _ = source_manifest(
            projected.messages, request.old_messages, request.retained_message_refs,
            capture_reader_names=request.source_capture_reader_names,
            fresh_tool_observation_policy=request.fresh_tool_observation_policy,
            fresh_tool_message_refs=request.fresh_tool_message_refs,
            fresh_text_fragment_policy=request.fresh_text_fragment_policy,
            fresh_text_excluded_tool_names=request.source_reader_names,
        )
        return bool(
            request.summary_partition.pending.stop == len(manifest["dropped"])
            and request.summary_counted_input_tokens + request.summary_output_tokens
            <= config.profile.context_window
            and sum(planner.count_model_message(m, config.profile)
                    for m in request.retained_messages)
            + planner.summary_reservation(config.profile)
            <= request.budget.input_capacity
        )

    return admit
