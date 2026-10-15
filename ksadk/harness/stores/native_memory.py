"""Thread-safe in-process RunStore; durable behavior belongs to SQLiteRunStore."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from threading import RLock

from ksadk.harness.native.fork import ForkCreation
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

from .native_command_settlement import settle_terminal_commands
from .native_contract import (
    EffectAlreadyClaimed,
    PendingCommandConflict,
    ScopeMismatch,
    StoreConflict,
    check_commit,
    check_fence,
    check_fork,
    check_initial,
    check_intent,
    check_receipt,
    check_scope,
    check_turn,
    detached,
    duplicate_fork,
    encode,
)
from .native_memory_archive import SnapshotArchive


@dataclass(frozen=True, slots=True)
class _CommitMemo:
    payload_sha256: str
    snapshot_ref: int


@dataclass
class _Record:
    snapshot: str
    fence: int = 0
    events: list[str] = field(default_factory=list)
    event_ids: set[str] = field(default_factory=set)
    commands: dict[str, tuple[str, bool]] = field(default_factory=dict)
    intents: dict[str, str] = field(default_factory=dict)
    receipts: dict[str, str] = field(default_factory=dict)
    commits: dict[str, _CommitMemo] = field(default_factory=dict)
    snapshot_archive: SnapshotArchive = field(default_factory=SnapshotArchive)


class MemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], _Record] = {}
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = RLock()
        self._closed = False

    def _open(self) -> None:
        if self._closed:
            raise StoreConflict("store is closed")

    def _record(self, scope: RunScope, run_id: str) -> _Record:
        self._open()
        record = self._runs.get((encode(scope), run_id))
        if record is None:
            if any(key[1] == run_id for key in self._runs):
                raise ScopeMismatch("run authorization scope mismatch")
            raise KeyError("run not found")
        return record

    def create(self, snapshot: RunSnapshot) -> None:
        snapshot = detached(snapshot)
        check_initial(snapshot)
        if snapshot.fork_source is not None or snapshot.fork_command is not None:
            raise StoreConflict("fork requires atomic branch creation")
        key = (encode(snapshot.scope), snapshot.run_id)
        with self._lock:
            self._open()
            if key in self._runs:
                raise StoreConflict("run already exists")
            self._runs[key] = _Record(snapshot=encode(snapshot))

    def load(self, scope: RunScope, run_id: str) -> RunSnapshot:
        with self._lock:
            return RunSnapshot.model_validate_json(self._record(scope, run_id).snapshot)

    def session_head(self, scope: RunScope, session_id: str) -> RunSnapshot | None:
        with self._lock:
            self._open()
            run_id = self._sessions.get((encode(scope), session_id))
            return self.load(scope, run_id) if run_id is not None else None

    def create_turn(self, snapshot: RunSnapshot, *, expected_parent_run_id: str | None) -> None:
        snapshot = detached(snapshot)
        key = (encode(snapshot.scope), snapshot.session_id)
        with self._lock:
            self._open()
            head = self.session_head(snapshot.scope, snapshot.session_id)
            check_turn(snapshot, head, expected_parent_run_id)
            self.create(snapshot)
            self._sessions[key] = snapshot.run_id

    def create_fork(self, snapshot: RunSnapshot) -> ForkCreation:
        snapshot = detached(snapshot)
        with self._lock:
            self._open()
            ref = snapshot.fork_source
            if ref is None:
                raise StoreConflict("fork source boundary required")
            source = self.load(snapshot.scope, ref.source_run_id)
            check_fork(snapshot, source)
            session_key = (encode(snapshot.scope), snapshot.session_id)
            run_key = (encode(snapshot.scope), snapshot.run_id)
            if run_key in self._runs and snapshot.fork_command is not None:
                return duplicate_fork(snapshot, self.load(snapshot.scope, snapshot.run_id))
            if session_key in self._sessions or run_key in self._runs:
                raise StoreConflict("fork destination already exists")
            record = _Record(snapshot=encode(snapshot))
            self._runs[run_key] = record
            self._sessions[session_key] = snapshot.run_id
            return ForkCreation(snapshot=detached(snapshot), created=True)

    def acquire(self, scope: RunScope, run_id: str) -> int:
        with self._lock:
            record = self._record(scope, run_id)
            record.fence += 1
            return record.fence

    def enqueue(self, scope: RunScope, run_id: str, command: RunCommand) -> CommandReceipt:
        command = detached(command)
        raw = encode(command)
        with self._lock:
            record = self._record(scope, run_id)
            existing = record.commands.get(command.command_id)
            if existing is not None:
                if existing[0] != raw:
                    raise StoreConflict("command ID payload mismatch")
                return CommandReceipt(command_id=command.command_id, status="duplicate")
            if RunSnapshot.model_validate_json(record.snapshot).phase.terminal:
                return CommandReceipt(
                    command_id=command.command_id, status="rejected", reason="run is terminal"
                )
            record.commands[command.command_id] = (raw, False)
            return CommandReceipt(command_id=command.command_id, status="accepted")

    def commands(self, scope: RunScope, run_id: str) -> tuple[RunCommand, ...]:
        with self._lock:
            pending = [
                RunCommand.model_validate_json(raw)
                for raw, consumed in self._record(scope, run_id).commands.values()
                if not consumed
            ]
            return tuple(sorted(pending, key=lambda item: -item.priority))

    def commit(
        self, scope: RunScope, batch: CommitBatch, *, expected_revision: int, fencing_token: int
    ) -> RunSnapshot:
        return self._commit(scope, batch, expected_revision=expected_revision,
                            fencing_token=fencing_token)

    def commit_child_input(self, scope, batch, *, expected_revision, fencing_token, delivery):
        return self._commit(scope, batch, expected_revision=expected_revision,
                            fencing_token=fencing_token, delivery=detached(delivery))

    def _commit(self, scope, batch, *, expected_revision, fencing_token, delivery=None):
        batch = detached(batch)
        check_scope(scope, batch.snapshot)
        raw = encode(batch)
        if delivery is not None:
            raw += "\n" + encode(delivery)
        payload_sha256 = hashlib.sha256(raw.encode()).hexdigest()
        with self._lock:
            record = self._record(scope, batch.snapshot.run_id)
            check_fence(record.fence, fencing_token)
            prior = record.commits.get(batch.commit_id)
            if prior is not None:
                if prior.payload_sha256 != payload_sha256:
                    raise StoreConflict("commit ID payload mismatch")
                return RunSnapshot.model_validate_json(
                    record.snapshot_archive.restore(prior.snapshot_ref)
                )
            current = RunSnapshot.model_validate_json(record.snapshot)
            check_commit(current, batch, expected_revision)
            child_record, child_raw = None, None
            if delivery is not None:
                from ksadk.harness.native.child_input import ChildInputRejected, check_delivery

                parent_entry = record.commands.get(batch.consumed_command_id)
                if parent_entry is None or parent_entry[1]:
                    raise StoreConflict("command is missing or already consumed")
                try:
                    child_record = self._record(delivery.target.scope, delivery.target.run_id)
                except (KeyError, ScopeMismatch) as exc:
                    raise ChildInputRejected("child_input_origin_changed") from exc
                check_delivery(
                    current, RunSnapshot.model_validate_json(child_record.snapshot),
                    RunCommand.model_validate_json(parent_entry[0]), delivery,
                    parent_commands=self.commands(scope, current.run_id),
                    child_commands=self.commands(delivery.target.scope, delivery.target.run_id),
                )
                child_raw = encode(delivery.command)
                existing = child_record.commands.get(delivery.command.command_id)
                if existing is not None and existing[0] != child_raw:
                    raise StoreConflict("child command ID payload mismatch")
            if (
                (batch.snapshot.phase.terminal or batch.require_no_pending_commands)
                and batch.consumed_command_id is None
                and any(not consumed for _raw, consumed in record.commands.values())
            ):
                raise PendingCommandConflict(
                    "pending command prevents guarded commit"
                    if batch.require_no_pending_commands
                    else "pending command prevents terminal commit"
                )
            command = None
            if batch.consumed_command_id is not None:
                command = record.commands.get(batch.consumed_command_id)
                if command is None or command[1]:
                    raise StoreConflict("command is missing or already consumed")
            discarded_ids = ()
            if batch.snapshot.phase.terminal and command is not None:
                batch, discarded_ids = settle_terminal_commands(
                    current, batch, self.commands(scope, batch.snapshot.run_id)
                )
                check_commit(current, batch, expected_revision)
            if any(item.event_id in record.event_ids for item in batch.events):
                raise StoreConflict("event ID already committed")
            for intent in batch.intents:
                existing = record.intents.get(intent.operation_key)
                check_intent(
                    EffectIntent.model_validate_json(existing) if existing else None, intent
                )
            proposed_keys = {item.operation_key for item in batch.intents}
            for receipt in batch.receipts:
                if (
                    receipt.operation_key not in record.intents
                    and receipt.operation_key not in proposed_keys
                ):
                    raise StoreConflict("receipt has no prepared intent")
                existing = record.receipts.get(receipt.operation_key)
                check_receipt(
                    EffectReceipt.model_validate_json(existing) if existing else None, receipt
                )
            # Serialize every proposed write before mutating any committed data.
            saved = encode(batch.snapshot)
            events = [encode(item) for item in batch.events]
            intents = [(item.operation_key, encode(item)) for item in batch.intents]
            receipts = [(item.operation_key, encode(item)) for item in batch.receipts]
            memo = _CommitMemo(payload_sha256, record.snapshot_archive.store(saved))
            record.snapshot = saved
            record.events.extend(events)
            record.event_ids.update(item.event_id for item in batch.events)
            for key, value in intents:
                record.intents.setdefault(key, value)  # never reset running to prepared
            for key, value in receipts:
                record.receipts.setdefault(key, value)
            if command is not None:
                record.commands[batch.consumed_command_id] = (command[0], True)
            for command_id in discarded_ids:
                record.commands[command_id] = (record.commands[command_id][0], True)
            record.commits[batch.commit_id] = memo
            if child_record is not None:
                child_record.commands.setdefault(delivery.command.command_id, (child_raw, False))
            return RunSnapshot.model_validate_json(saved)

    def events(self, scope: RunScope, run_id: str, after_seq: int = 0) -> tuple[NativeEvent, ...]:
        with self._lock:
            return tuple(
                event
                for raw in self._record(scope, run_id).events
                if (event := NativeEvent.model_validate_json(raw)).seq > after_seq
            )

    def get_intent(self, scope: RunScope, run_id: str, operation_key: str) -> EffectIntent | None:
        with self._lock:
            raw = self._record(scope, run_id).intents.get(operation_key)
            return EffectIntent.model_validate_json(raw) if raw else None

    def claim_intent(
        self, scope: RunScope, run_id: str, operation_key: str, *, fencing_token: int,
        require_no_pending_commands: bool = False,
    ) -> EffectIntent:
        with self._lock:
            record = self._record(scope, run_id)
            check_fence(record.fence, fencing_token)
            if RunSnapshot.model_validate_json(record.snapshot).phase.terminal:
                raise StoreConflict("terminal run is immutable")
            if require_no_pending_commands and any(
                not consumed for _raw, consumed in record.commands.values()
            ):
                raise PendingCommandConflict("pending command prevents effect claim")
            raw = record.intents.get(operation_key)
            if raw is None:
                raise KeyError("intent not found")
            intent = EffectIntent.model_validate_json(raw)
            if intent.status != "prepared" or operation_key in record.receipts:
                raise EffectAlreadyClaimed("effect is running or already has a receipt")
            claimed = intent.model_copy(update={"status": "running"})
            record.intents[operation_key] = encode(claimed)
            return detached(claimed)

    def get_receipt(self, scope: RunScope, run_id: str, operation_key: str) -> EffectReceipt | None:
        with self._lock:
            raw = self._record(scope, run_id).receipts.get(operation_key)
            return EffectReceipt.model_validate_json(raw) if raw else None

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self) -> MemoryRunStore:
        self._open()
        return self

    def __exit__(self, *_args) -> None:
        self.close()
