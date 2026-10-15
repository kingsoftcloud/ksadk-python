"""Pure capacity estimates for a declared, canonical artifact read batch.

Estimates are never receipts or facts. They apply only to ordinary completion
of the current read-only group without intervening commands or host changes.
The real ContextPlanner remains the hard admission authority after execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .artifact_read_contract import ARTIFACT_READ_DEFINITION
from .effective_configuration import effective_run_configuration
from .machine import next_transition, reduce_tool
from .model import ModelToolCall
from .ports import ToolResult, digest
from .state import RunPhase
from .tool_budget import model_instructions, model_tool_surface, model_turn_control
from .transitions import commit_transition

ARTIFACT_CAPACITY_POLICY = "sealed-artifact-batch/v1"


@dataclass(frozen=True)
class ArtifactGroupCapacity:
    """An estimate in the bound planner's units, not persistent execution state."""

    source_units: int
    result_message_units: int
    fixed_tokens: int
    summary_tokens: int
    group_size: int


def _declared_group(snapshot):
    index = next((i for i in range(len(snapshot.messages)-1, -1, -1)
                  if snapshot.messages[i].get("role") == "assistant"
                  and snapshot.messages[i].get("tool_calls")), None)
    if index is None:
        return None
    declared = snapshot.messages[index]
    calls = declared["tool_calls"]
    tail = snapshot.messages[index+1:]
    if (not calls or len({c["id"] for c in calls}) != len(calls)
            or len(tail) + len(snapshot.pending_tools) != len(calls)):
        return None
    try:
        arguments = [json.loads(c["function"]["arguments"]) for c in calls]
        # Duplicate requests can produce a separate loop-warning observation;
        # their future framing is deliberately outside this bounded quote.
        if len({json.dumps(a, sort_keys=True) for a in arguments}) != len(calls):
            return None
        if any(c["function"]["name"] != "ArtifactRead" for c in calls):
            return None
        if any(m.get("role") != "tool" or m.get("tool_call_id") != c["id"]
               or m.get("receipt_goal_revision") != snapshot.goal_revision
               or json.loads(m["content"]).get("status") != "ok"
               for m, c in zip(tail, calls)):
            return None
        expected = tuple(ModelToolCall(call_id=c["id"], name=c["function"]["name"],
                                       arguments=a).model_dump(mode="json")
                         for c, a in zip(calls[len(tail):], arguments[len(tail):]))
        if expected != snapshot.pending_tools:
            return None
        if len(calls) == 1:
            previous = next((m for m in reversed(snapshot.messages[:index])
                             if m.get("role") == "assistant" and m.get("tool_calls")), None)
            if previous:
                prior = previous["tool_calls"][-1]["function"]
                if (prior["name"] == "ArtifactRead"
                        and json.loads(prior["arguments"]) == arguments[0]):
                    return None
    except (KeyError, TypeError, ValueError):
        return None
    return declared, tail


def artifact_group_capacity(snapshot, configuration, driver):
    """Quote no-command batch completion using the same pure native reducers.

    Receipts, progress digits, capped receipt digests, tool-budget controls and
    real summary framing are remeasured rather than estimated by fixed magic
    constants or by guessing the future operation-key revision width.
    """
    if (snapshot.configuration_digest != configuration.digest
            or snapshot.profile_digest != digest(configuration.profile)
            or snapshot.context_binding != driver.binding):
        raise ValueError("source_read_configuration_mismatch")
    planner = driver.planner
    if (planner is None or not planner.policy.read_working_set_count
            or snapshot.phase != RunPhase.EXECUTING_TOOLS):
        return None
    group = _declared_group(snapshot)
    if group is None or not snapshot.pending_tools or not snapshot.pending_intent:
        return None
    declared, sealed = group
    # The planner's retained-source count is a bound on source identities,
    # not on the number of atomic call groups. The original declared count
    # is a safe upper bound without pretending overlapping sources are new.
    if len(declared["tool_calls"]) > planner.policy.read_working_set_count:
        return None
    config = effective_run_configuration(configuration, snapshot)

    installed = [t for t in config.tools if t.name == ARTIFACT_READ_DEFINITION.name]
    if (len(installed) != 1 or installed[0].model_copy(update={"feedback_profile_digest": None})
            != ARTIFACT_READ_DEFINITION):
        return None
    if (snapshot.tool_calls + snapshot.descendant_tool_calls + len(snapshot.pending_tools)
            > config.limits.max_tool_calls):
        return None
    # An intervening committed command breaks this finite no-command estimate.
    # It must not be disguised by deriving new keys from an old pending intent.
    if snapshot.pending_intent != f"{snapshot.run_id}:effect:{snapshot.revision}":
        return None
    current = snapshot.model_copy(deep=True)
    first = True
    remaining = len(snapshot.pending_tools)
    while current.pending_tools:
        before = (current.model_copy(update={"revision": current.revision-1,
                                             "pending_intent": None}) if first else current)
        prepared = next_transition(before, config)
        if (prepared is None or len(prepared.intents) != 1
                or prepared.intents[0].kind != "tool"):
            return None
        intent = prepared.intents[0]
        if first and intent.operation_key != current.pending_intent:
            return None
        current = prepared.snapshot
        calls = intent.payload.get("calls") or [intent.payload["call"]]
        updated = current
        for call in calls:
            # Only these synthetic result *shapes* enter a detached reducer.
            # Content is not exposed, persisted, or attributed to any tool.
            result = ToolResult(status="ok", content={"capacity_call": call["call_id"]})
            updated = reduce_tool(
                updated.model_copy(update={"pending_intent": intent.operation_key}),
                result, definition=installed[0],
            )
        current = commit_transition(current, updated, "effect.received").snapshot
        first = False
    completed = next_transition(current, config)
    if completed is None or completed.snapshot.phase != RunPhase.PREPARING_CONTEXT:
        return None
    current = completed.snapshot
    fixed = []
    # Novelty can advance anywhere from zero to the remaining call count. A
    # real tokenizer need not assign monotonic costs across numeric digit runs.
    for progress in range(snapshot.observed_progress_revision,
                          snapshot.observed_progress_revision + remaining + 1):
        projected = current.model_copy(update={"observed_progress_revision": progress})
        fixed.append(planner.fixed_request_cost(
            instructions=model_instructions(projected, config),
            continuity=driver.continuity(projected, config), contract=config.contract,
            tools=model_tool_surface(projected, config),
            turn_control=model_turn_control(projected, config),
        ))
    profile = config.profile
    def count(message):
        return planner.count_model_message(message, profile)

    goal = {"role": "user", "content": snapshot.goal}
    # Existing sealed receipts are still in this original group. They cannot
    # disappear from the denominator or be silently charged as older history.
    size = len(declared["tool_calls"])
    available = (profile.context_window - profile.max_output_tokens - max(fixed)
                 - count(goal) - count(declared)
                 - sum(count(m) for m in snapshot.messages if m.get("role") == "system"))
    results = min(available - planner.summary_reservation(profile),
                  planner.policy.read_working_set_max_tokens - count(declared),
                  planner.policy.read_working_set_total_max_tokens - count(declared))
    result_units = min(results // size,
                       (results - sum(count(m) for m in sealed)) // remaining)
    return ArtifactGroupCapacity(
        source_units=available // 2 // size, result_message_units=result_units,
        fixed_tokens=max(fixed), summary_tokens=planner.summary_reservation(profile),
        group_size=size,
    )
