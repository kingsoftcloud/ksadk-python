"""Pure quote for complete next requests after a canonical Search group.

No summaries, old receipts or source bytes are changed. Synthetic result shapes
exist only in a detached reducer projection, never as Run facts. The estimate
covers ordinary completion without intervening commands or host changes.
"""

import json
from dataclasses import dataclass

from .artifact_search_contract import ARTIFACT_SEARCH_DEFINITION
from .context_runtime_contracts import CompactionHistory
from .delivery_review import delivery_review_messages
from .effective_configuration import effective_run_configuration
from .machine import next_transition, reduce_parallel_readonly_tools, reduce_tool
from .model import ModelToolCall
from .ports import ToolResult, digest
from .state import RunPhase
from .tool_budget import model_instructions, model_tool_surface, model_turn_control
from .transitions import commit_transition

SEARCH_CAPACITY_POLICY = "complete-next-search-request/v1"


@dataclass(frozen=True)
class SearchGroupCapacity:
    result_message_units: int
    group_size: int
    pending_count: int
    maximum_fixed_and_history_units: int


def _declared_group(snapshot):
    index = next(
        (
            i
            for i in range(len(snapshot.messages) - 1, -1, -1)
            if snapshot.messages[i].get("role") == "assistant"
            and snapshot.messages[i].get("tool_calls")
        ),
        None,
    )
    if index is None:
        return None
    declared, tail = snapshot.messages[index], snapshot.messages[index + 1 :]
    try:
        calls = declared["tool_calls"]
        if (
            not calls
            or len({c["id"] for c in calls}) != len(calls)
            or len(tail) + len(snapshot.pending_tools) != len(calls)
            or any(c["function"]["name"] != "ArtifactSearch" for c in calls)
        ):
            return None
        arguments = [json.loads(c["function"]["arguments"]) for c in calls]
        if len({json.dumps(a, sort_keys=True) for a in arguments}) != len(calls):
            return None
        if any(
            m.get("role") != "tool"
            or m.get("tool_call_id") != c["id"]
            or m.get("receipt_goal_revision") != snapshot.goal_revision
            or json.loads(m["content"]).get("status") != "ok"
            for m, c in zip(tail, calls)
        ):
            return None
        expected = tuple(
            ModelToolCall(call_id=c["id"], name="ArtifactSearch", arguments=a).model_dump(
                mode="json"
            )
            for c, a in zip(calls[len(tail) :], arguments[len(tail) :])
        )
        if expected != snapshot.pending_tools:
            return None
        if len(calls) == 1:
            previous = next(
                (
                    m
                    for m in reversed(snapshot.messages[:index])
                    if m.get("role") == "assistant" and m.get("tool_calls")
                ),
                None,
            )
            if previous:
                prior = previous["tool_calls"][-1]["function"]
                if (
                    prior["name"] == "ArtifactSearch"
                    and json.loads(prior["arguments"]) == arguments[0]
                ):
                    return None
    except (KeyError, TypeError, ValueError):
        return None
    return declared, tail


def search_group_capacity(snapshot, configuration, driver):
    if (
        snapshot.configuration_digest != configuration.digest
        or snapshot.profile_digest != digest(configuration.profile)
        or snapshot.context_binding != driver.binding
    ):
        raise ValueError("search_configuration_mismatch")
    planner = driver.planner
    if (
        planner is None
        or snapshot.phase != RunPhase.EXECUTING_TOOLS
        or not snapshot.pending_tools
        or not snapshot.pending_intent
        or snapshot.force_compaction
    ):
        return None
    group = _declared_group(snapshot)
    if group is None:
        return None
    declared, sealed = group
    config = effective_run_configuration(configuration, snapshot)
    installed = [t for t in config.tools if t.name == "ArtifactSearch"]
    if (
        len(installed) != 1
        or installed[0].model_copy(update={"feedback_profile_digest": None})
        != ARTIFACT_SEARCH_DEFINITION.model_copy(update={"feedback_profile_digest": None})
        or snapshot.tool_calls + snapshot.descendant_tool_calls + len(snapshot.pending_tools)
        > config.limits.max_tool_calls
        or snapshot.pending_intent != f"{snapshot.run_id}:effect:{snapshot.revision}"
    ):
        return None
    current, first = snapshot.model_copy(deep=True), True
    pending_ids = {c["call_id"] for c in snapshot.pending_tools}
    while current.pending_tools:
        before = (
            current.model_copy(update={"revision": current.revision - 1, "pending_intent": None})
            if first
            else current
        )
        transition = next_transition(before, config)
        if (
            transition is None
            or not transition.intents
            or transition.intents[0].kind != "tool"
        ):
            return None
        intent = transition.intents[0]
        if first and intent.operation_key != current.pending_intent:
            return None
        current = transition.snapshot
        calls = intent.payload.get("calls") or [intent.payload["call"]]
        if len(calls) > 1:
            updated = reduce_parallel_readonly_tools(
                current,
                ToolResult(status="ok", content={
                    "batch_results": [ToolResult(status="ok", content="").model_dump(
                        mode="json"
                    ) for _ in calls],
                }),
                calls,
                definitions=config.tools,
            )
        else:
            updated = reduce_tool(
                current,
                ToolResult(status="ok", content=""),
                definition=installed[0],
            )
        current = commit_transition(current, updated, "effect.received").snapshot
        first = False
    transition = next_transition(current, config)
    if transition is None or transition.snapshot.phase != RunPhase.PREPARING_CONTEXT:
        return None
    current = transition.snapshot
    history = CompactionHistory.model_validate(current.compaction_history)
    remaining, costs = len(pending_ids), []
    for progress in range(
        snapshot.observed_progress_revision, snapshot.observed_progress_revision + remaining + 1
    ):
        projected = current.model_copy(update={"observed_progress_revision": progress})
        plan = planner.plan(
            source_revision=projected.revision,
            model_turn=projected.model_calls,
            profile=config.profile,
            instructions=model_instructions(projected, config),
            tools=model_tool_surface(projected, config),
            context_tools=config.tools,
            messages=delivery_review_messages(projected),
            continuity=driver.continuity(projected, config),
            contract=config.contract,
            history=history,
            force_compaction=False,
            turn_control=model_turn_control(projected, config),
            allow_compaction=False,
        )
        if plan.action != "ready" or plan.prepared is None:
            return None
        synthetic = [
            m
            for m in plan.prepared.messages
            if m.get("role") == "tool" and m.get("tool_call_id") in pending_ids
        ]
        if len(synthetic) != remaining:
            return None
        costs.append(
            plan.prepared.counted_input_tokens
            - sum(planner.count_model_message(m, config.profile) for m in synthetic)
        )
    available = config.profile.context_window - config.profile.max_output_tokens - max(costs)
    sealed_cost = sum(planner.count_model_message(m, config.profile) for m in sealed)
    units = min(available // remaining, (available + sealed_cost) // len(declared["tool_calls"]))
    return SearchGroupCapacity(units, len(declared["tool_calls"]), remaining, max(costs))
