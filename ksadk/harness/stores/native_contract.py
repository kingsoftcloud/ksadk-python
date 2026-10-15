"""Native RunStore contract and shared commit validation.

The control plane can enqueue commands without owning the worker lease. Worker
state commits and effect claims require the current fencing epoch. Commands,
event IDs and commit IDs are namespaced by the complete scope and run ID.
"""

from __future__ import annotations

import json
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from ksadk.harness.native.fork import ForkCreation
from ksadk.harness.native.restore_contract import StoreRestoreAudit
from ksadk.harness.native.state import (
    CommandReceipt,
    CommitBatch,
    EffectIntent,
    EffectReceipt,
    NativeEvent,
    RunCommand,
    RunScope,
    RunSnapshot,
)


class StoreConflict(RuntimeError):
    """A write contradicts the committed state or its fencing epoch."""


class PendingCommandConflict(StoreConflict):
    """A control command was accepted before an uncommanded terminal commit."""


class ScopeMismatch(StoreConflict):
    """The supplied authorization scope does not own the addressed run."""


class EffectAlreadyClaimed(StoreConflict):
    """The operation may already have executed; callers must reconcile it."""


class LeaseBusy(StoreConflict):
    """A different local worker still owns the run's unexpired lease."""


@runtime_checkable
class RunStore(Protocol):
    def create(self, snapshot: RunSnapshot) -> None: ...
    def session_head(self, scope: RunScope, session_id: str) -> RunSnapshot | None: ...
    def create_turn(self, snapshot: RunSnapshot, *, expected_parent_run_id: str | None) -> None: ...
    def load(self, scope: RunScope, run_id: str) -> RunSnapshot: ...
    def acquire(self, scope: RunScope, run_id: str) -> int: ...
    def enqueue(self, scope: RunScope, run_id: str, command: RunCommand) -> CommandReceipt: ...
    def commands(self, scope: RunScope, run_id: str) -> tuple[RunCommand, ...]: ...
    def commit(
        self, scope: RunScope, batch: CommitBatch, *, expected_revision: int, fencing_token: int
    ) -> RunSnapshot:
        """Atomically persist the transition and its command dispositions.

        A command-caused terminal also records and consumes remaining accepted
        commands as discarded. The returned snapshot includes these Store-owned
        events; replaying the original request returns that same actual result.
        Uncommanded finalization must conflict with pending controls instead.
        """
        ...
    def events(
        self, scope: RunScope, run_id: str, after_seq: int = 0
    ) -> tuple[NativeEvent, ...]: ...
    def get_intent(
        self, scope: RunScope, run_id: str, operation_key: str
    ) -> EffectIntent | None: ...
    def claim_intent(
        self, scope: RunScope, run_id: str, operation_key: str, *, fencing_token: int,
        require_no_pending_commands: bool = False,
    ) -> EffectIntent: ...
    def get_receipt(
        self, scope: RunScope, run_id: str, operation_key: str
    ) -> EffectReceipt | None: ...
    def close(self) -> None: ...


@runtime_checkable
class ChildInputRunStore(Protocol):
    """Optional atomic parent command disposition and owned child inbox enqueue."""

    def commit_child_input(self, scope, batch, *, expected_revision, fencing_token, delivery): ...


@runtime_checkable
class ForkingRunStore(Protocol):
    """Optional atomic transcript branch creation; never emulate with two writes."""

    def create_fork(self, snapshot: RunSnapshot) -> ForkCreation: ...


@runtime_checkable
class LeasedRunStore(Protocol):
    """Optional durable ownership; legacy in-memory stores keep epoch fencing."""

    def acquire_lease(
        self, scope: RunScope, run_id: str, *, owner_id: str, ttl_seconds: float
    ) -> int: ...
    def renew_lease(
        self,
        scope: RunScope,
        run_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> None: ...
    def release_lease(
        self, scope: RunScope, run_id: str, *, owner_id: str, fencing_token: int
    ) -> None: ...


@runtime_checkable
class RestoreAuditStore(Protocol):
    """Optional diagnostics on an admitted restore, outside execution state."""

    def record_restore(
        self,
        scope: RunScope,
        run_id: str,
        *,
        reader_version: str,
        configuration_digest: str,
        fencing_token: int | None,
    ) -> None: ...
    def restore_audits(self, scope: RunScope, run_id: str) -> tuple[StoreRestoreAudit, ...]: ...


Model = TypeVar("Model", bound=BaseModel)


def encode(value: BaseModel) -> str:
    """Canonical JSON also detaches all nested mutable payloads at write time."""
    try:
        return json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StoreConflict("store values must be valid JSON") from exc


def detached(value: Model) -> Model:
    """Validate nested values even if a caller used unvalidated model_copy."""
    try:
        return type(value).model_validate_json(encode(value))
    except ValueError as exc:
        raise StoreConflict("invalid store contract value") from exc


def check_scope(scope: RunScope, snapshot: RunSnapshot) -> None:
    if scope != snapshot.scope:
        raise ScopeMismatch("run authorization scope mismatch")


def check_fence(current: int, provided: int) -> None:
    if type(provided) is not int or provided < 1 or current != provided:
        raise StoreConflict("stale or missing fencing token")


def check_initial(snapshot: RunSnapshot) -> None:
    if snapshot.revision != 0 or snapshot.event_seq != 0:
        raise StoreConflict("new run requires revision and event_seq zero")
    if snapshot.initial_goal_digest is not None and not snapshot.matches_initial_goal(
        snapshot.goal
    ):
        raise StoreConflict("initial_goal_digest must bind the original goal")


def check_turn(
    snapshot: RunSnapshot, head: RunSnapshot | None, expected_parent_run_id: str | None
) -> None:
    """Validate under the store's write lock/transaction, before any insertion.

    Session history is assembled from ``head`` by the host. The CAS binds that
    immutable terminal parent to its child; a racing writer must retry from the
    new head rather than reuse previously read history.
    """
    check_initial(snapshot)
    if snapshot.fork_source is not None or snapshot.fork_command is not None:
        raise StoreConflict("fork requires atomic branch creation")
    if (head.run_id if head else None) != expected_parent_run_id:
        raise StoreConflict("session head compare-and-swap failed")
    if snapshot.parent_run_id != expected_parent_run_id:
        raise StoreConflict("parent_run_id does not match expected session head")
    if head is not None:
        if not head.phase.terminal:
            raise StoreConflict("session already has an active turn")
        if snapshot.turn_id == head.turn_id or snapshot.run_id == head.run_id:
            raise StoreConflict("new turn requires fresh run and turn identities")


def check_fork(snapshot: RunSnapshot, source: RunSnapshot) -> None:
    """Bind history and fresh state under the same lock as destination insertion."""
    from ksadk.harness.native.fork import (
        command_run_id,
        fork_request_digest,
        fork_source,
        project_fork_history,
    )

    check_initial(snapshot)
    check_scope(snapshot.scope, source)
    if not source.phase.terminal:
        raise StoreConflict("fork requires a terminal source run")
    if snapshot.fork_source != fork_source(source):
        raise StoreConflict("fork source boundary changed")
    if (snapshot.session_id == source.session_id or snapshot.run_id == source.run_id
            or snapshot.turn_id == source.turn_id or snapshot.parent_run_id is not None):
        raise StoreConflict("fork requires fresh session, run and turn identities")
    if snapshot.messages != (
        *project_fork_history(
            source, snapshot.run_id, profile_digest=snapshot.profile_digest,
            configuration_digest=snapshot.configuration_digest,
        ), {"role": "user", "content": snapshot.goal},
    ):
        raise StoreConflict("fork history does not match the fixed source boundary")
    if snapshot.initial_goal_digest is None:
        raise StoreConflict("fork requires a bound initial goal")
    command = snapshot.fork_command
    if command is not None and (
        snapshot.run_id != command_run_id(snapshot.scope, source.run_id, command.command_id)
        or command.request_digest != fork_request_digest(snapshot)
    ):
        raise StoreConflict("fork command identity or request binding mismatch")
    # Only destination-host authority and historical messages may be provided.
    # Anything else is fresh execution state, including counters and verdicts.
    host_fields = {
        "scope", "run_id", "turn_id", "session_id", "goal", "fork_source", "fork_command",
        "messages",
        "initial_goal_digest", "context_binding", "task_contract", "profile_digest",
        "contract_digest", "configuration_digest", "origin", "activation_resource_ref",
        "workspace_revision", "workspace_observation_status", "workspace_observation_reason",
    }
    fresh = RunSnapshot(
        scope=snapshot.scope, run_id=snapshot.run_id, turn_id=snapshot.turn_id,
        session_id=snapshot.session_id, goal=snapshot.goal,
    )
    if snapshot.model_dump(exclude=host_fields) != fresh.model_dump(exclude=host_fields):
        raise StoreConflict("fork cannot inherit execution state")


def duplicate_fork(proposed: RunSnapshot, current: RunSnapshot) -> ForkCreation:
    """Read the immutable command fact, never compare a steered active goal."""
    if (proposed.fork_command is None or current.fork_command != proposed.fork_command
            or current.fork_source != proposed.fork_source
            or current.session_id != proposed.session_id
            or current.initial_goal_digest != proposed.initial_goal_digest):
        raise StoreConflict("fork command was already used with another request")
    return ForkCreation(snapshot=current, created=False)


def check_commit(current: RunSnapshot, batch: CommitBatch, expected_revision: int) -> None:
    target = batch.snapshot
    if batch.require_no_pending_commands and batch.consumed_command_id is not None:
        raise StoreConflict("no-command barrier cannot consume a command")
    if current.phase.terminal:
        raise StoreConflict("terminal run is immutable")
    if current.revision != expected_revision or target.revision != expected_revision + 1:
        raise StoreConflict("snapshot revision compare-and-swap failed")
    for name in (
        "scope",
        "run_id",
        "session_id",
        "turn_id",
        "parent_run_id",
        "fork_source",
        "fork_command",
        "origin",
        "initial_goal_digest",
    ):
        if getattr(current, name) != getattr(target, name):
            raise StoreConflict(f"immutable run identity changed: {name}")
    if target.event_seq != current.event_seq + len(batch.events):
        raise StoreConflict("snapshot event_seq does not match appended events")
    for seq, event in enumerate(batch.events, start=current.event_seq + 1):
        if event.seq != seq:
            raise StoreConflict("event sequence must be contiguous")
        if event.run_id != current.run_id or event.turn_id != current.turn_id:
            raise StoreConflict("event run/turn identity mismatch")
    for label, values in (
        ("event_id", [item.event_id for item in batch.events]),
        ("intent", [item.operation_key for item in batch.intents]),
        ("receipt", [item.operation_key for item in batch.receipts]),
    ):
        if len(values) != len(set(values)):
            raise StoreConflict(f"duplicate {label} in commit")
    if any(intent.status != "prepared" for intent in batch.intents):
        raise StoreConflict("new intents must be prepared; use claim_intent to run")


def check_intent(existing: EffectIntent | None, proposed: EffectIntent) -> None:
    if existing is not None and (
        existing.kind != proposed.kind or existing.payload != proposed.payload
    ):
        raise StoreConflict("operation key cannot be reused for a different effect")


def check_receipt(existing: EffectReceipt | None, proposed: EffectReceipt) -> None:
    if existing is not None and existing != proposed:
        raise StoreConflict("operation receipt is immutable")
