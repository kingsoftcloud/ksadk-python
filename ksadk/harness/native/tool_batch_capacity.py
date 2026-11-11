"""Detached source-batch projections across durable, out-of-order member receipts.

These projections price model input only. They never persist, execute a tool,
or authorize recovery. Canonical receipts and the final planner remain intact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .machine import next_transition, reduce_parallel_readonly_tools, reduce_tool
from .model import ModelToolCall
from .ports import ToolResult
from .state import RunPhase, RunSnapshot
from .tool_scheduling import scheduled_tool_calls
from .transitions import commit_transition


@dataclass(frozen=True)
class SourceToolGroup:
    declared: dict
    sealed: tuple[dict, ...]
    active: tuple[dict, ...]
    baseline: RunSnapshot


def source_request_identity(snapshot, request, configuration) -> bool:
    """A source request must belong to the current parent or its exact member."""
    if (request.scope != snapshot.scope or request.run_id != snapshot.run_id
            or not snapshot.pending_intent):
        return False
    calls = scheduled_tool_calls(snapshot, configuration)
    raw = request.call.model_dump(mode="json")
    if raw not in calls:
        return False
    return request.operation_key == snapshot.pending_intent or (
        len(calls) > 1 and request.operation_key
        == f"{snapshot.pending_intent}:call:{calls.index(raw) + 1}"
    )


def declared_source_group(snapshot: RunSnapshot, name: str) -> SourceToolGroup | None:
    """Validate the closed prefix and any durably observed active members.

    pending_tools is not shortened until a parallel parent receipt closes.
    Active member commits each add one revision; any other committed update
    therefore invalidates this no-intervening-change quote.
    """
    index = next((i for i in range(len(snapshot.messages) - 1, -1, -1)
                  if snapshot.messages[i].get("role") == "assistant"
                  and snapshot.messages[i].get("tool_calls")), None)
    if index is None or not snapshot.pending_intent or not snapshot.pending_tools:
        return None
    declared = snapshot.messages[index]
    tail = snapshot.messages[index + 1:]
    try:
        calls = declared["tool_calls"]
        arguments = [json.loads(c["function"]["arguments"]) for c in calls]
        if (not calls or len({c["id"] for c in calls}) != len(calls)
                or any(c["function"]["name"] != name for c in calls)
                or len({json.dumps(a, sort_keys=True) for a in arguments}) != len(calls)):
            return None
        expected = tuple(ModelToolCall(call_id=c["id"], name=name, arguments=a)
                         .model_dump(mode="json") for c, a in zip(calls, arguments))
        closed = len(calls) - len(snapshot.pending_tools)
        if closed < 0 or expected[closed:] != snapshot.pending_tools or len(tail) < closed:
            return None
        positions = {c["id"]: i for i, c in enumerate(calls)}
        observed = [positions[m["tool_call_id"]] for m in tail]
        if (observed != sorted(set(observed)) or observed[:closed] != list(range(closed))
                or any(m.get("role") != "tool"
                       or m.get("receipt_goal_revision") != snapshot.goal_revision
                       or json.loads(m["content"]).get("status") != "ok" for m in tail)):
            return None
        active = tuple(tail[closed:])
        if any(m.get("receipt_ref") != snapshot.pending_intent
               or m.get("receipt_tool_call") != snapshot.tool_calls
               + positions[m["tool_call_id"]] - closed + 1 for m in active):
            return None
        revision = snapshot.revision - len(active)
        if snapshot.pending_intent != f"{snapshot.run_id}:effect:{revision}":
            return None
        if len(calls) == 1:
            previous = next((m for m in reversed(snapshot.messages[:index])
                             if m.get("role") == "assistant" and m.get("tool_calls")), None)
            if previous:
                prior = previous["tool_calls"][-1]["function"]
                if prior["name"] == name and json.loads(prior["arguments"]) == arguments[0]:
                    return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    # Only this detached estimate omits active content. Its real measured cost
    # is charged separately; every authoritative message stays unchanged.
    baseline = snapshot.model_copy(update={
        "messages": (*snapshot.messages[:index + 1], *tail[:closed]),
        "revision": revision, "event_seq": snapshot.event_seq - len(active),
    }, deep=True)
    return SourceToolGroup(declared, tuple(tail), active, baseline)


def project_source_group(group: SourceToolGroup, configuration) -> RunSnapshot | None:
    """Finish the bounded group with synthetic shapes using actual reducers."""
    current, first = group.baseline, True
    active_ids = {m["tool_call_id"] for m in group.active}
    while current.pending_tools:
        before = (current.model_copy(update={"revision": current.revision - 1,
                                             "pending_intent": None}) if first else current)
        prepared = next_transition(before, configuration)
        if not prepared or not prepared.intents or prepared.intents[0].kind != "tool":
            return None
        intent = prepared.intents[0]
        calls = intent.payload.get("calls") or [intent.payload["call"]]
        parallel = intent.payload.get("parallel_readonly") is True
        if first and intent.operation_key != current.pending_intent:
            return None
        if (tuple(calls) != scheduled_tool_calls(before, configuration)
                or intent.payload.get("parallel_delegation") is True
                or not active_ids.issubset({c["call_id"] for c in calls})):
            return None
        if parallel:
            members = prepared.intents[1:]
            if len(members) != len(calls) or any(
                member.kind != "tool" or member.payload != {"call": call}
                or member.operation_key != f"{intent.operation_key}:call:{i + 1}"
                for i, (member, call) in enumerate(zip(members, calls))
            ):
                return None
        elif active_ids or len(prepared.intents) != 1:
            return None
        current = prepared.snapshot
        results = [ToolResult(status="ok", content={"capacity_call": c["call_id"]})
                   for c in calls]
        if parallel:
            # A real parallel group commits each member before closing its
            # parent. Preserve those revision boundaries in future keys.
            for _ in calls:
                current = commit_transition(current, current, "tool.member_settled").snapshot
            updated = reduce_parallel_readonly_tools(
                current, ToolResult(status="ok", content={
                    "batch_results": [r.model_dump(mode="json") for r in results],
                }), calls, definitions=configuration.tools,
            )
        else:
            definition = next(t for t in configuration.tools if t.name == calls[0]["name"])
            updated = reduce_tool(current, results[0], definition=definition)
        current = commit_transition(current, updated, "effect.received").snapshot
        first, active_ids = False, set()
    completed = next_transition(current, configuration)
    if completed is None or completed.snapshot.phase != RunPhase.PREPARING_CONTEXT:
        return None
    return completed.snapshot
