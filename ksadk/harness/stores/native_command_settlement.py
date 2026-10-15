"""Settle accepted, unexecuted commands inside the terminal Store transaction."""

from __future__ import annotations

import hashlib

from ksadk.harness.native.state import CommitBatch, NativeEvent, RunCommand, RunSnapshot

TERMINALS = {"run.completed", "run.failed", "run.canceled", "run.interrupted"}


def settle_terminal_commands(
    current: RunSnapshot, batch: CommitBatch, pending: tuple[RunCommand, ...]
) -> tuple[CommitBatch, tuple[str, ...]]:
    """The caller validates the original commit and holds its write lock.

    Ordinary finalization cannot discard steering: its pending-command conflict
    remains the Store's responsibility. Only a command-caused terminal settles
    the remaining accepted inbox. The original request payload remains the
    idempotency key; these additions are facts read under the same transaction.
    """
    if not batch.snapshot.phase.terminal or batch.consumed_command_id is None:
        return batch, ()
    discarded = tuple(
        command for command in pending if command.command_id != batch.consumed_command_id
    )
    if not discarded:
        return batch, ()
    additions = tuple(
        NativeEvent(
            event_id=(
                f"{batch.commit_id}:command.discarded:"
                f"{hashlib.sha256(command.command_id.encode()).hexdigest()}"
            ),
            run_id=current.run_id,
            turn_id=current.turn_id,
            seq=1,  # Assigned with the complete transaction below.
            kind="command.discarded",
            payload={
                "command_id": command.command_id,
                "kind": command.kind,
                "status": "discarded",
                "reason": "run_terminal",
                "terminal_reason": batch.snapshot.reason,
                "terminating_command_id": batch.consumed_command_id,
            },
        )
        for command in discarded
    )
    index = next(
        (i for i, event in enumerate(batch.events) if event.kind in TERMINALS),
        len(batch.events),
    )
    entries = (*batch.events[:index], *additions, *batch.events[index:])
    events = tuple(
        event.model_copy(update={"seq": seq})
        for seq, event in enumerate(entries, start=current.event_seq + 1)
    )
    updated = batch.model_copy(update={
        "snapshot": batch.snapshot.model_copy(
            update={"event_seq": current.event_seq + len(events)}
        ),
        "events": events,
    })
    return updated, tuple(command.command_id for command in discarded)
