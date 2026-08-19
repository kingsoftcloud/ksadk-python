# -*- coding: utf-8 -*-
"""Agent Kernel Inbox/Run 状态机与事务不变量（Phase 1 Task 3）。

Inbox 固定 ``accepted -> claimed -> completed|discarded``；
Run 固定 ``pending -> running -> paused|waiting|completed|failed|cancelled|interrupted``，
终态 first-wins：进入终态后禁止任何再 transition。
"""

from __future__ import annotations

from enum import StrEnum

from ksadk.kernel.errors import InvalidCommandError


class InboxState(StrEnum):
    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    DISCARDED = "discarded"


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_RUN_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    }
)

ACTIVE_RUN_STATES = frozenset({RunState.RUNNING, RunState.PAUSED, RunState.WAITING})

INBOX_TRANSITIONS: dict[InboxState, frozenset[InboxState]] = {
    InboxState.ACCEPTED: frozenset({InboxState.CLAIMED, InboxState.DISCARDED}),
    InboxState.CLAIMED: frozenset({InboxState.COMPLETED, InboxState.DISCARDED}),
    InboxState.COMPLETED: frozenset(),
    InboxState.DISCARDED: frozenset(),
}

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.INTERRUPTED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.PAUSED,
            RunState.WAITING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.PAUSED: frozenset(
        {
            RunState.WAITING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.WAITING: frozenset(
        {
            # A durable InteractionResolved returns the run to active execution
            # before its adapter produces the next runtime event.
            RunState.RUNNING,
            RunState.PAUSED,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.INTERRUPTED: frozenset(),
}


def is_terminal_run(state: RunState) -> bool:
    return state in TERMINAL_RUN_STATES


def is_active_run(state: RunState) -> bool:
    return state in ACTIVE_RUN_STATES


def assert_inbox_transition(current: InboxState, target: InboxState) -> None:
    if target not in INBOX_TRANSITIONS[current]:
        raise InvalidCommandError(
            f"illegal inbox transition {current.value} -> {target.value}",
            details={"current": current.value, "target": target.value},
        )


def assert_run_transition(current: RunState | None, target: RunState) -> None:
    """终态 first-wins：current 已是终态时，任何 target 都非法。"""

    if current is not None and is_terminal_run(current):
        raise InvalidCommandError(
            f"run already reached terminal state {current.value}",
            details={"current": current.value, "target": target.value},
        )
    if is_terminal_run(target):
        return
    if current is None:
        if target is not RunState.PENDING:
            raise InvalidCommandError(
                f"a new run must start at pending, got {target.value}",
                details={"target": target.value},
            )
        return
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidCommandError(
            f"illegal run transition {current.value} -> {target.value}",
            details={"current": current.value, "target": target.value},
        )


__all__ = [
    "InboxState",
    "RunState",
    "TERMINAL_RUN_STATES",
    "ACTIVE_RUN_STATES",
    "INBOX_TRANSITIONS",
    "RUN_TRANSITIONS",
    "is_terminal_run",
    "is_active_run",
    "assert_inbox_transition",
    "assert_run_transition",
]
