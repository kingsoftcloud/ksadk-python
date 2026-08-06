from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from ksadk.sessions import SessionEvent


@dataclass
class RuntimeGovernanceState:
    max_turns: int = 0
    max_tool_calls: int = 0
    max_consecutive_tool_failures: int = 0
    max_consecutive_approval_denials: int = 0
    max_consecutive_compact_failures: int = 0
    turn_count: int = 0
    tool_calls: int = 0
    consecutive_tool_failures: int = 0
    consecutive_approval_denials: int = 0
    consecutive_compact_failures: int = 0


class RuntimeCircuitOpen(RuntimeError):
    def __init__(self, reason: str, message: str, *, metadata: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.metadata = dict(metadata or {})


def _int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)


def _runtime_governance_from_env() -> RuntimeGovernanceState:
    return RuntimeGovernanceState(
        max_turns=_int_env("KSADK_MAX_TURNS", 0),
        max_tool_calls=_int_env("KSADK_MAX_TOOL_CALLS", 0),
        max_consecutive_tool_failures=_int_env("KSADK_MAX_CONSECUTIVE_TOOL_FAILURES", 0),
        max_consecutive_approval_denials=_int_env("KSADK_MAX_CONSECUTIVE_APPROVAL_DENIALS", 0),
        max_consecutive_compact_failures=_int_env("KSADK_MAX_CONSECUTIVE_COMPACT_FAILURES", 0),
    )


def _governance_error(
    reason: str, message: str, state: RuntimeGovernanceState
) -> RuntimeCircuitOpen:
    return RuntimeCircuitOpen(
        reason,
        message,
        metadata={
            "reason": reason,
            "max_turns": state.max_turns,
            "max_tool_calls": state.max_tool_calls,
            "max_consecutive_tool_failures": state.max_consecutive_tool_failures,
            "max_consecutive_approval_denials": state.max_consecutive_approval_denials,
            "max_consecutive_compact_failures": state.max_consecutive_compact_failures,
            "turn_count": state.turn_count,
            "tool_calls": state.tool_calls,
            "consecutive_tool_failures": state.consecutive_tool_failures,
            "consecutive_approval_denials": state.consecutive_approval_denials,
            "consecutive_compact_failures": state.consecutive_compact_failures,
        },
    )


def _governance_record_turn_start(state: RuntimeGovernanceState) -> None:
    state.turn_count += 1
    if state.max_turns and state.turn_count > state.max_turns:
        raise _governance_error("max_turns_exceeded", "runtime max_turns limit exceeded", state)


def _governance_record_tool_call(state: RuntimeGovernanceState) -> None:
    state.tool_calls += 1
    if state.max_tool_calls and state.tool_calls > state.max_tool_calls:
        raise _governance_error(
            "max_tool_calls_exceeded", "runtime max_tool_calls limit exceeded", state
        )


def _governance_record_tool_result(state: RuntimeGovernanceState, output: Any) -> None:
    failed = isinstance(output, Mapping) and output.get("ok") is False
    state.consecutive_tool_failures = state.consecutive_tool_failures + 1 if failed else 0
    if (
        state.max_consecutive_tool_failures
        and state.consecutive_tool_failures >= state.max_consecutive_tool_failures
    ):
        raise _governance_error(
            "consecutive_tool_failures", "runtime consecutive tool failure limit exceeded", state
        )


def _governance_record_approval_response(
    state: RuntimeGovernanceState, approval: Mapping[str, Any]
) -> None:
    approved = bool(approval.get("approved") or approval.get("approve"))
    state.consecutive_approval_denials = 0 if approved else state.consecutive_approval_denials + 1
    if (
        state.max_consecutive_approval_denials
        and state.consecutive_approval_denials >= state.max_consecutive_approval_denials
    ):
        raise _governance_error(
            "consecutive_approval_denials",
            "runtime consecutive approval denial limit exceeded",
            state,
        )


def _governance_record_compact_failure(state: RuntimeGovernanceState) -> None:
    state.consecutive_compact_failures += 1
    if (
        state.max_consecutive_compact_failures
        and state.consecutive_compact_failures >= state.max_consecutive_compact_failures
    ):
        raise _governance_error(
            "consecutive_compact_failures",
            "runtime consecutive compact failure limit exceeded",
            state,
        )


def _governance_record_compact_success(state: RuntimeGovernanceState) -> None:
    state.consecutive_compact_failures = 0


async def _compact_conversation_history_with_governance(
    governance: RuntimeGovernanceState | None,
    **kwargs: Any,
) -> SessionEvent | None:
    from ksadk.conversations.runtime_compaction import compact_conversation_history

    try:
        checkpoint = await compact_conversation_history(**kwargs)
    except Exception:
        if governance is not None:
            _governance_record_compact_failure(governance)
        raise
    if checkpoint is not None and governance is not None:
        _governance_record_compact_success(governance)
    return checkpoint


def _tool_observability_metadata(
    tool_name: str, output: Any, *, duration_ms: int | None = None
) -> dict[str, Any]:
    output_text = (
        json.dumps(output, ensure_ascii=False, sort_keys=True)
        if isinstance(output, Mapping)
        else str(output)
    )
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "duration_ms": int(duration_ms or 0),
        "output_chars": len(output_text),
        "truncated": False,
        "persisted": False,
        "exit_code": None,
        "error_type": "",
    }
    if isinstance(output, Mapping):
        persisted = output.get("persisted") or output.get("persisted_outputs")
        metadata.update(
            {
                "truncated": any(
                    bool(output.get(key))
                    for key in (
                        "truncated",
                        "stdout_truncated",
                        "stderr_truncated",
                        "results_truncated",
                    )
                ),
                "persisted": bool(persisted),
                "exit_code": output.get("exit_code"),
                "error_type": str(output.get("error_type") or ""),
            }
        )
    return metadata
