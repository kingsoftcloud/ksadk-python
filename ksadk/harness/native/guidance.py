"""Current-turn guidance at sealed boundaries, separate from replacing a goal.

Reference: ZCode 872ad960 turn-guide-drain.ts, turn-tools.ts and turn-stop.ts.
One input is injected after a completed tool batch or text-only response, then
gets one effective model step before the next guide. The existing Store owns
admission FIFO, command identity and atomic delivery facts.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, ValidationError

from .completion_text import candidate_model_message
from .state import Contract, RunCommand, RunPhase, RunSnapshot


class GuidanceInput(Contract):
    text: Annotated[str, Field(min_length=1, max_length=32768, pattern=r"\S")]
    expected_goal_revision: int = Field(strict=True, ge=0)


def runnable_commands(snapshot, commands, *, guidance_enabled: bool):
    """Defer guides without hiding cancellation, approval or explicit steering."""
    boundary = (
        snapshot.pending_intent is None
        and snapshot.phase
        in {
            RunPhase.CREATED,
            RunPhase.PREPARING_CONTEXT,
            RunPhase.VERIFYING,
            RunPhase.FINALIZING,
        }
        and (not snapshot.pending_guidance_ids or snapshot.phase == RunPhase.FINALIZING)
    )
    return tuple(
        command
        for command in commands
        if command.kind != "guide" or not guidance_enabled or boundary
    )


def apply_guidance(snapshot: RunSnapshot, command: RunCommand):
    if snapshot.phase.terminal:
        return snapshot, "run_is_terminal"
    try:
        guidance = GuidanceInput.model_validate(command.payload)
    except ValidationError:
        return snapshot, "invalid_guidance"
    if guidance.expected_goal_revision != snapshot.goal_revision:
        return snapshot, "stale_goal_revision"
    if snapshot.pending_guidance_ids:
        return snapshot, "guidance_previous_unconsumed"
    if snapshot.phase == RunPhase.FINALIZING and snapshot.reason == "effect_outcome_unknown":
        return snapshot, "guidance_effect_unreconciled"
    if not runnable_commands(snapshot, (command,), guidance_enabled=True):
        return snapshot, "guidance_not_at_boundary"
    messages = snapshot.messages
    if snapshot.candidate is not None:
        messages = (*messages, candidate_model_message(snapshot))
    message = {
        "role": "user",
        "kind": "user_guidance",
        "content": guidance.text,
        "input_id": command.command_id,
        "input_delivery": "guide",
        "input_goal_revision": snapshot.goal_revision,
    }
    updated = snapshot.model_copy(
        update={
            "phase": RunPhase.PREPARING_CONTEXT,
            "messages": (*messages, message),
            "pending_guidance_ids": (command.command_id,),
            "candidate": None,
            "candidate_replay": None,
            "verification": None,
            "verification_tool_calls": None,
            "delivery_review": None,
            "stop_hook_state": None,
            "prepared_context": None,
            "compaction_request": None,
            "force_compaction": False,
            "continuations": 0,
            "retry_not_before": 0,
            "outcome": None,
            "final_text": "",
            "reason": None,
        }
    )
    return RunSnapshot.model_validate_json(updated.model_dump_json()), "applied"
