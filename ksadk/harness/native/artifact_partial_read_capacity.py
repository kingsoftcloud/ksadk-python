"""Pure admission of one executable ArtifactRead plus budget-refused suffix.

The projected result is never a receipt or a Store fact. This limited quote
assumes ordinary completion without intervening commands or host changes.
"""

from __future__ import annotations

import json

from .artifact_read_contract import ARTIFACT_READ_DEFINITION
from .artifact_read_capacity import _declared_group
from .context_artifact_page import artifact_page_observation
from .context_runtime_contracts import CompactionHistory
from .delivery_review import delivery_review_messages
from .effective_configuration import effective_run_configuration
from .machine import next_transition, reduce_tool
from .ports import ToolResult, digest
from .state import RunPhase
from .tool_budget import model_instructions, model_tool_surface, model_turn_control
from .transitions import commit_transition

PARTIAL_ARTIFACT_CAPACITY_POLICY = "one-read-and-budget-refused-suffix/v1"


def _valid_arguments(arguments):
    return (
        isinstance(arguments, dict)
        and not set(arguments).difference({"uri", "offset_chars", "limit_chars"})
        and isinstance(arguments.get("uri"), str)
        and bool(arguments["uri"])
        and type(arguments.get("offset_chars", 0)) is int
        and arguments.get("offset_chars", 0) >= 0
        and type(arguments.get("limit_chars", 16000)) is int
        and 1 <= arguments.get("limit_chars", 16000) <= 32000
    )


def partial_artifact_group_admission(snapshot, configuration, driver):
    """Return a pure result predicate only for exactly one executable call.

    Every prior message and completed history remains in the projected state.
    Actual native transitions generate all unexecuted suffix observations; no
    arithmetic estimate or fabricated receipt stands in for their framing.
    """
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
        or not snapshot.pending_intent
        or len(snapshot.pending_tools) < 2
        or snapshot.pending_intent != f"{snapshot.run_id}:effect:{snapshot.revision}"
        or snapshot.revision < 1
        or snapshot.event_seq < 1
    ):
        return None
    group = _declared_group(snapshot)
    if group is None:
        return None
    declared, _sealed = group
    if len(declared["tool_calls"]) > planner.policy.read_working_set_count:
        return None
    arguments = [json.loads(c["function"]["arguments"]) for c in declared["tool_calls"]]
    if not all(_valid_arguments(a) for a in arguments):
        return None
    config = effective_run_configuration(configuration, snapshot)
    installed = [t for t in config.tools if t.name == ARTIFACT_READ_DEFINITION.name]
    if (
        len(installed) != 1
        or installed[0].model_copy(update={"feedback_profile_digest": None})
        != ARTIFACT_READ_DEFINITION
        or config.limits.max_tool_calls - snapshot.tool_calls - snapshot.descendant_tool_calls != 1
        or snapshot.model_calls + snapshot.descendant_model_calls >= config.limits.max_model_calls
    ):
        return None
    current = snapshot.model_copy(deep=True)
    before = current.model_copy(
        update={
            "revision": current.revision - 1,
            "event_seq": current.event_seq - 1,
            "pending_intent": None,
        }
    )
    prepared = next_transition(before, config)
    if (
        prepared is None
        or len(prepared.intents) != 1
        or prepared.intents[0].kind != "tool"
        or prepared.intents[0].operation_key != current.pending_intent
        or prepared.intents[0].payload != {"call": current.pending_tools[0]}
        or prepared.snapshot != current
    ):
        return None
    call = current.pending_tools[0]
    available = {r["uri"]: r for r in current.artifact_refs}
    ref = available.get(call["arguments"]["uri"])
    if ref is None:
        return None

    def admit(result):
        if (
            not isinstance(result, ToolResult)
            or result.status not in {"ok", "error"}
            or result != ToolResult(status=result.status, content=result.content)
            or not isinstance(result.content, dict)
        ):
            return False
        if result.status == "ok":
            page = result.content
            if any(page.get(k) != v for k, v in ref.items()) or not (
                page.get("content") or page.get("complete") is True
            ):
                return False
            message = {
                "role": "tool",
                "tool_call_id": call["call_id"],
                "content": json.dumps({"status": "ok", "content": page}, ensure_ascii=False),
            }
            if artifact_page_observation(message, arguments=call["arguments"]) is None:
                return False
        elif result.content.get("code") != "artifact_page_budget_unavailable" or set(
            result.content
        ).difference({"code", "capacity_scope", "page_limits", "next_read_shape"}):
            return False
        updated = reduce_tool(current, result, definition=installed[0])
        received = commit_transition(current, updated, "effect.received")
        completed = next_transition(received.snapshot, config)
        if (
            completed is None
            or completed.intents
            or completed.snapshot.phase != RunPhase.PREPARING_CONTEXT
            or len(completed.events) != 1
            or completed.events[0].kind != "budget.exhausted"
            or completed.snapshot.pending_tools
            or completed.snapshot.tool_calls != current.tool_calls + 1
            or completed.snapshot.model_calls != current.model_calls
            or completed.snapshot.compaction_history != current.compaction_history
            or completed.snapshot.messages[: len(current.messages)] != current.messages
        ):
            return False
        projected = completed.snapshot
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
            history=CompactionHistory.model_validate(projected.compaction_history),
            force_compaction=False,
            turn_control=model_turn_control(projected, config),
            allow_compaction=False,
        )
        return bool(
            plan.action == "ready"
            and plan.prepared is not None
            and plan.prepared.counted_input_tokens + config.profile.max_output_tokens
            <= config.profile.context_window
        )

    return admit
