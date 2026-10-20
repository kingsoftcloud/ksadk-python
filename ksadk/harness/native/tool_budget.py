"""Project the remaining tool budget into model requests and tool batches."""

from __future__ import annotations

import json

from .call_budget import call_room, remaining_calls, share_calls
from .delivery_review import (
    REVIEW_INSTRUCTIONS,
    delivery_review_readers,
    delivery_review_tool,
    review_pending,
)
from .ports import RunConfiguration, ToolBudget, ToolDefinition
from .state import CommitBatch, RunPhase, RunSnapshot, move
from .task_policy import (
    web_observation_due,
    web_verification_only,
)
from .transitions import commit_transition
from .turn_control import (
    EXHAUSTED_INSTRUCTION as _EXHAUSTED_INSTRUCTION,
)
from .turn_control import (
    FINALIZATION_INSTRUCTION as _FINALIZATION_INSTRUCTION,
)
from .turn_control import (
    OBSERVATION_DUE_INSTRUCTION as _OBSERVATION_DUE_INSTRUCTION,
)
from .turn_control import (
    VERIFICATION_INSTRUCTION as _VERIFICATION_INSTRUCTION,
)
from .turn_control import (
    ModelTurnControl,
)


def dispatch_tool_budget(
    snapshot: RunSnapshot, config: RunConfiguration, *, batch_size: int = 1, index: int = 0
) -> ToolBudget:
    """One authoritative net grant for dispatch and interrupted receipt admission."""
    parallel = batch_size > 1
    reserve = (max(config.limits.finalization_reserve_model_calls, 1) if parallel
               else config.limits.finalization_reserve_model_calls)
    model_slots = remaining_calls(config.limits.max_model_calls, snapshot.model_budget_claimed, reserve)
    tool_slots = remaining_calls(config.limits.max_tool_calls, snapshot.tool_budget_claimed, batch_size)

    return ToolBudget(
        max_model_calls_remaining=share_calls(model_slots, batch_size, index),
        max_tool_calls_remaining=share_calls(tool_slots, batch_size, index),
        parent_finalization_reserved=parallel or bool(reserve),
        depth=config.delegation_depth, max_depth=config.limits.max_subagent_depth,
    )


def tools_exhausted(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    return not call_room(config.limits.max_tool_calls, snapshot.tool_budget_claimed)


def has_admitted_task_tools(config: RunConfiguration) -> bool:
    allowed = config.contract.allowed_tool_names
    return any(allowed is None or tool.name in allowed for tool in config.tools)


def task_tool_budget_exhausted(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    # A text-only role can deliberately have a zero tool allowance. It has
    # not consumed a task tool budget and must not enter soft finalization.
    # The numerical fence remains unconditional at the execution boundary.
    return has_admitted_task_tools(config) and tools_exhausted(snapshot, config)


def finalization_only(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    reserve = config.limits.finalization_reserve_model_calls
    return bool(
        reserve
        and config.limits.max_model_calls is not None
        and config.limits.max_model_calls - snapshot.model_budget_claimed
        <= reserve
    )


def wall_delivery_only(snapshot: RunSnapshot, config: RunConfiguration) -> bool:
    reserve = config.limits.finalization_reserve_wall_seconds
    quote = snapshot.execution_budget_observation
    # A preparation quote remains conservative after a model/tool claim. Its
    # counts need not match; crossing the window cannot reopen exploration.
    return bool(
        reserve and config.limits.max_wall_seconds is not None and quote is not None
        and quote.run_id == snapshot.run_id and quote.goal_revision == snapshot.goal_revision
        and quote.wall_seconds_remaining is not None
        and quote.wall_seconds_remaining <= reserve
    )


def model_turn_control(snapshot: RunSnapshot, config: RunConfiguration) -> ModelTurnControl | None:
    if review_pending(snapshot):
        return None
    reason = (
        "task_tools_exhausted"
        if task_tool_budget_exhausted(snapshot, config)
        else "model_budget_reserved"
        if finalization_only(snapshot, config)
        else "browser_observation_due"
        if web_observation_due(snapshot, config)
        else "browser_verification_reserved"
        if web_verification_only(snapshot, config)
        else "wall_delivery_reserved"
        if wall_delivery_only(snapshot, config)
        else None
    )
    return (
        ModelTurnControl(
            reason=reason,
            final_submission_required=config.final_submission_schema is not None,
            admitted_tool_names=(
                tuple(tool.name for tool in model_tool_surface(snapshot, config))
                if reason in {"browser_observation_due", "browser_verification_reserved", "wall_delivery_reserved"}
                else None
            ),
        )
        if reason
        else None
    )


def model_tool_surface(
    snapshot: RunSnapshot, config: RunConfiguration
) -> tuple[ToolDefinition, ...]:
    from .final_submission import final_submission_tool

    if review_pending(snapshot):
        return (*delivery_review_readers(snapshot, config), delivery_review_tool())
    submission = final_submission_tool(config)
    if tools_exhausted(snapshot, config) or finalization_only(snapshot, config):
        task_tools = ()
    elif web_observation_due(snapshot, config):
        task_tools = tuple(tool for tool in config.tools if tool.observation_role == "browser")
    elif web_verification_only(snapshot, config):
        observed = snapshot.last_browser_observation
        fresh = (
            isinstance(observed, dict)
            and type(observed.get("tool_call")) is int
            and observed["tool_call"] >= (snapshot.last_web_mutation_tool_call or 0)
            and snapshot.unresolved_interaction_observation is None
        )
        task_tools = tuple(
            tool
            for tool in config.tools
            if tool.observation_role == "browser" or (fresh and tool.read_only)
        )
    elif wall_delivery_only(snapshot, config):
        from .delivery_artifacts import scoped_delivery_reader

        task_tools = tuple(
            scoped or tool
            for tool in config.tools
            if (scoped := scoped_delivery_reader(tool, snapshot)) is not None or tool.delivery_write
        )
    else:
        task_tools = config.tools
    allowed = config.contract.allowed_tool_names
    if allowed is not None:
        task_tools = tuple(tool for tool in task_tools if tool.name in allowed)
    return (*task_tools, submission) if submission is not None else task_tools


def model_tool_catalog(
    snapshot: RunSnapshot, config: RunConfiguration
) -> tuple[ToolDefinition, ...]:
    """Keep the task request prefix independent of current execution admission."""
    if config.profile.tool_catalog_policy != "stable-task/v1" or review_pending(snapshot):
        return model_tool_surface(snapshot, config)
    from .final_submission import final_submission_tool

    allowed = config.contract.allowed_tool_names
    tools = tuple(tool for tool in config.tools if allowed is None or tool.name in allowed)
    submission = final_submission_tool(config)
    if wall_delivery_only(snapshot, config):
        from .delivery_artifacts import scoped_delivery_reader

        tools = tuple(
            scoped if (scoped := scoped_delivery_reader(tool, snapshot)) is not None else tool
            for tool in tools
        )
    return (*tools, submission) if submission is not None else tools


def model_instructions(snapshot: RunSnapshot, config: RunConfiguration) -> str:
    if review_pending(snapshot):
        return REVIEW_INSTRUCTIONS
    submission_instruction = (
        "For final delivery, call submit_final_output with arguments matching its schema. "
        "A plain-text final response cannot satisfy this structured output contract."
        if config.final_submission_schema is not None
        else ""
    )
    if not task_tool_budget_exhausted(snapshot, config):
        if not finalization_only(snapshot, config):
            if web_observation_due(snapshot, config):
                return "\n\n".join(
                    part
                    for part in (
                        config.instructions,
                        _OBSERVATION_DUE_INSTRUCTION,
                        submission_instruction,
                    )
                    if part
                )
            if web_verification_only(snapshot, config):
                return "\n\n".join(
                    part
                    for part in (
                        config.instructions,
                        _VERIFICATION_INSTRUCTION,
                        submission_instruction,
                    )
                    if part
                )
            return "\n\n".join(
                part for part in (config.instructions, submission_instruction) if part
            )
        return "\n\n".join(
            part
            for part in (config.instructions, _FINALIZATION_INSTRUCTION, submission_instruction)
            if part
        )
    return "\n\n".join(
        part
        for part in (config.instructions, _EXHAUSTED_INSTRUCTION, submission_instruction)
        if part
    )


def refused_pending_tools(
    snapshot: RunSnapshot,
    *,
    reason: str = "tool_budget_exhausted",
    available_tools: tuple[str, ...] | None = None,
) -> tuple[dict, ...]:
    """Pair unexecuted calls with host-authored errors; never debit the tool budget."""
    return tuple(
        {
            "role": "tool",
            "tool_call_id": call["call_id"],
            "content": json.dumps(
                {
                    "status": "error",
                    "content": {
                        "reason": reason,
                        "executed": False,
                        **(
                            {"available_tools": list(available_tools)}
                            if available_tools is not None
                            else {}
                        ),
                    },
                },
                ensure_ascii=False,
            ),
        }
        for call in snapshot.pending_tools
    )


def refuse_pending_batch(snapshot: RunSnapshot) -> CommitBatch:
    """Commit protocol-paired refusals before the next no-tool model request."""
    refused = refused_pending_tools(snapshot)
    updated = move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "pending_tools": (),
            "messages": (*snapshot.messages, *refused),
        }
    )
    return commit_transition(
        snapshot,
        updated,
        "budget.exhausted",
        {
            "reason": "tool_budget_exhausted",
            "refused_call_ids": [call["call_id"] for call in snapshot.pending_tools],
        },
    )
