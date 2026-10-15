"""Warn once per unchanged tool streak, only after complete protocol batches.

Reference: OpenCode fe3f3a41 processor.ts:29,358-379 uses a threshold of
three repeated inputs; ZCode 872ad960 turn-tool-warnings.ts:52-79 injects
anomaly reminders. This independent adaptation also requires unchanged results
and only emits an observation: it never approves, stops, retries, or resets a
budget. Actual result payload timestamps remain meaningful; envelope identities,
timestamps and usage do not contribute to the comparison.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from .model import ModelToolCall
from .ports import ToolResult
from .state import Contract

REPEATED_TOOL_THRESHOLD = 3
Messages = tuple[dict[str, JsonValue], ...]
_OBSERVATIONS = {
    "tool_loop_observation",
    "model_recovery_observation",
    "verification_observation",
    "continuation_observation",
    "background_child_observation",
}


class ToolLoopWarning(Contract):
    reason: Literal["repeated_tool_no_progress"] = "repeated_tool_no_progress"
    threshold: Literal[3] = REPEATED_TOOL_THRESHOLD
    observed_count: int
    signature: str


class ToolProgressFeedback(Contract):
    messages: Messages
    warning: ToolLoopWarning | None = None


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _calls(message: dict) -> tuple[ModelToolCall, ...]:
    parsed = []
    for raw in message["tool_calls"]:
        function = raw["function"]
        arguments = function["arguments"]
        parsed.append(
            ModelToolCall(
                call_id=raw["id"],
                name=function["name"],
                arguments=json.loads(arguments) if isinstance(arguments, str) else arguments,
            )
        )
    if not parsed or len({c.call_id for c in parsed}) != len(parsed):
        raise ValueError("invalid tool batch")
    return tuple(parsed)


def _observation(message: dict) -> str | None:
    value = message.get("content")
    blocks = []
    if isinstance(value, list) and value and value[0].get("type") == "text":
        blocks, value = value[1:], value[0].get("text")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, dict) and "status" in value:
        if value["status"] not in {"ok", "error"}:
            return None  # Missing, uncertain or superseded results do not prove repetition.
        value = {"status": value["status"], "content": value.get("content")}
    return _canonical({"result": value, "blocks": blocks})


@dataclass
class _Progress:
    signature: str = ""
    count: int = 0
    warned: bool = False
    valid: bool = True
    pending: tuple[ModelToolCall, ...] = ()
    results: dict[str, str | None] = field(default_factory=dict)
    last_error: bool = False

    def reset(self) -> None:
        self.signature, self.count, self.warned = "", 0, False
        self.last_error = False

    def record(self, call: ModelToolCall, observation: str | None) -> None:
        if observation is None:
            self.reset()
            return
        signature = hashlib.sha256(
            _canonical([call.name, call.arguments, observation]).encode()
        ).hexdigest()
        projected = json.loads(observation).get("result")
        self.last_error = isinstance(projected, dict) and projected.get("status") == "error"
        if signature == self.signature:
            self.count += 1
        else:
            self.signature, self.count, self.warned = signature, 1, False


def _scan(messages: Messages) -> _Progress:
    progress = _Progress()
    try:
        for message in messages:
            role = message.get("role")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if (
                    call_id not in {c.call_id for c in progress.pending}
                    or call_id in progress.results
                ):
                    raise ValueError("orphan or duplicate tool result")
                progress.results[call_id] = _observation(message)
                if len(progress.results) == len(progress.pending):
                    for call in progress.pending:
                        progress.record(call, progress.results[call.call_id])
                    progress.pending, progress.results = (), {}
                continue
            if progress.pending:
                raise ValueError("interleaved tool batch")
            if role == "assistant" and message.get("tool_calls"):
                progress.pending = _calls(message)
            elif role == "user":
                if message.get("kind") not in _OBSERVATIONS:
                    progress.reset()
                elif (
                    message.get("kind") == "tool_loop_observation"
                    and message.get("tool_progress_signature") == progress.signature
                    and progress.count >= REPEATED_TOOL_THRESHOLD
                ):
                    progress.warned = True
    except (KeyError, TypeError, ValueError, AttributeError):
        progress.valid = False
    return progress


def repeated_error_streak(messages: Messages) -> int:
    """Count identical, completed tool errors since the last changed observation."""
    progress = _scan(messages)
    if not progress.valid or progress.pending or not progress.last_error:
        return 0
    return progress.count


def _append_result(messages: Messages, call: ModelToolCall, result: ToolResult) -> Messages:
    progress = _scan(messages)
    if (
        not progress.valid
        or call not in progress.pending
        or call.call_id in progress.results
        or result.status not in {"ok", "error"}
    ):
        raise ValueError("tool_progress_invalid_pending_result")
    content = json.dumps({"status": result.status, "content": result.content}, ensure_ascii=False)
    message = {"role": "tool", "tool_call_id": call.call_id, "content": content}
    if result.content_blocks:
        message["content"] = [
            {"type": "text", "text": content},
            *(block.model_dump(mode="json") for block in result.content_blocks),
        ]
    return (*messages, message)


def tool_progress_feedback(
    messages: Messages,
    *,
    call: ModelToolCall | None = None,
    result: ToolResult | None = None,
) -> ToolProgressFeedback:
    """Project at ``tools.completed`` after appending all batch receipts.

    Prefer complete history as input. The optional pair supports projecting a
    newly finished result for a matching declared call; it cannot create a tool
    request. A still-pending or malformed batch produces no warning. The caller
    commits the returned messages and may emit ``warning`` as diagnostic data.
    This does not change the goal, acceptance, budgets or effect execution state.
    """
    if (call is None) != (result is None):
        raise ValueError("tool_progress_requires_call_and_result")
    if call is not None:
        messages = _append_result(messages, call, result)
    progress = _scan(messages)
    if (
        not progress.valid
        or progress.pending
        or progress.warned
        or progress.count < REPEATED_TOOL_THRESHOLD
    ):
        return ToolProgressFeedback(messages=messages)
    warning = ToolLoopWarning(observed_count=progress.count, signature=progress.signature)
    observation = {
        "role": "user",
        "kind": "tool_loop_observation",
        "trust": "untrusted",
        "tool_progress_signature": progress.signature,
        "content": _canonical(
            {
                "reason": warning.reason,
                "threshold": warning.threshold,
                "observed_count": warning.observed_count,
                "observation": "Recent completed tool calls repeated the same request and result. "
                "No changed observation was detected. Reassess the next action using existing "
                "evidence. This diagnostic does not change the user's goal, authorize a tool, "
                "or establish task completion.",
            }
        ),
    }
    return ToolProgressFeedback(messages=(*messages, observation), warning=warning)
