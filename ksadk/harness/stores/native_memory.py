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
    NotificationTurnCreation,
    PendingCommandConflict,
    ScopeMismatch,
    StoreConflict,
    TaskChainUsage,
    check_chain_turn,
    check_commit,
    check_fence,
    check_fork,
    check_initial,
    check_intent,
    check_notification_turn,
    check_receipt,
    check_scope,
    check_turn,
    detached,
    duplicate_fork,
    duplicate_notification_turn,
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
        self._task_chain_usage: dict[tuple[str, str], TaskChainUsage] = {}
        # Per-run settlement records: (scope_key, chain_id, run_id) -> usage dict.
        # A run is charged once regardless of how many terminal commits it has.
        self._task_chain_settled_runs: dict[tuple[str, str, str], dict] = {}
        # Independent chain registration: (scope_key, chain_id) -> set of run_ids.
        # Survives deletion of _task_chain_usage to detect corruption.
        self._task_chain_registrations: dict[tuple[str, str], set[str]] = {}
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
            check_chain_turn(snapshot, (self.load(snapshot.scope, member) for member in
                             self._task_chain_registrations.get(
                                 (encode(snapshot.scope), snapshot.task_chain_id), ())))
            # Stage all mutations: compute chain registration result BEFORE
            # publishing run/head so a rejection leaves no phantom state.
            chain_reg_ok = True
            chain_reg_error = None
            if snapshot.task_chain_id:
                chain_key = (encode(snapshot.scope), snapshot.task_chain_id)
                reg = self._task_chain_registrations.get(chain_key)
                if reg is not None and chain_key not in self._task_chain_usage:
                    chain_reg_ok = False
                    chain_reg_error = StoreConflict(
                        "task-chain registered but usage entry missing; fail-closed"
                    )
            if not chain_reg_ok:
                raise chain_reg_error
            # All validation passed; now mutate.
            self.create(snapshot)
            self._sessions[key] = snapshot.run_id
            if snapshot.task_chain_id:
                self._register_chain_membership(
                    encode(snapshot.scope), snapshot.task_chain_id, snapshot.run_id,
                )

    def _register_chain_membership(
        self, scope_key: str, task_chain_id: str, run_id: str,
    ) -> None:
        chain_key = (scope_key, task_chain_id)
        reg = self._task_chain_registrations.get(chain_key)
        if reg is None:
            # New chain: create registration + zero usage entry.
            self._task_chain_registrations[chain_key] = {run_id}
            if chain_key not in self._task_chain_usage:
                self._task_chain_usage[chain_key] = TaskChainUsage(
                    task_chain_id=task_chain_id,
                )
        else:
            # Existing chain: add run_id as member. Do NOT recreate usage if missing.
            if chain_key not in self._task_chain_usage:
                raise StoreConflict(
                    "task-chain registered but usage entry missing; fail-closed"
                )
            reg.add(run_id)

    def register_task_chain(
        self, scope: RunScope, task_chain_id: str, run_id: str,
    ) -> None:
        with self._lock:
            self._open()
            self._register_chain_membership(encode(scope), task_chain_id, run_id)

    def chain_is_registered(self, scope: RunScope, task_chain_id: str) -> bool:
        with self._lock:
            self._open()
            return (encode(scope), task_chain_id) in self._task_chain_registrations

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

    def create_notification_turn(
        self, snapshot: RunSnapshot, *, expected_parent_run_id: str | None
    ) -> NotificationTurnCreation:
        snapshot = detached(snapshot)
        with self._lock:
            self._open()
            session_key = (encode(snapshot.scope), snapshot.session_id)
            run_key = (encode(snapshot.scope), snapshot.run_id)
            if run_key in self._runs:
                # Idempotent repeat: return the already-created notification turn.
                return duplicate_notification_turn(
                    snapshot, self.load(snapshot.scope, snapshot.run_id)
                )
            head = self.session_head(snapshot.scope, snapshot.session_id)
            # Load the ORIGINAL source run (from the notification_command's
            # source_run_id, or the snapshot's parent_run_id) for binding
            # validation. expected_parent_run_id is the current session head.
            original_source_id = (
                snapshot.notification_command.source_run_id
                if snapshot.notification_command is not None
                else snapshot.parent_run_id
            )
            if not original_source_id:
                raise StoreConflict("notification turn requires a source run id")
            source = self.load(snapshot.scope, original_source_id)
            check_notification_turn(snapshot, source, head, expected_parent_run_id)
            check_chain_turn(snapshot, (self.load(snapshot.scope, member) for member in
                             self._task_chain_registrations.get(
                                 (encode(snapshot.scope), snapshot.task_chain_id), ())))
            # Stage chain registration check before mutating.
            if snapshot.task_chain_id:
                chain_key = (encode(snapshot.scope), snapshot.task_chain_id)
                reg = self._task_chain_registrations.get(chain_key)
                if reg is not None and chain_key not in self._task_chain_usage:
                    raise StoreConflict(
                        "task-chain registered but usage entry missing; fail-closed"
                    )
            self.create(snapshot)
            self._sessions[session_key] = snapshot.run_id
            # Register chain membership (inherits source's chain).
            if snapshot.task_chain_id:
                self._register_chain_membership(
                    encode(snapshot.scope), snapshot.task_chain_id, snapshot.run_id,
                )
            return NotificationTurnCreation(snapshot=detached(snapshot), created=True)

    def task_chain_usage(self, scope: RunScope, task_chain_id: str) -> TaskChainUsage:
        with self._lock:
            self._open()
            key = (encode(scope), task_chain_id)
            if key not in self._task_chain_usage:
                # If chain is registered but usage missing → corruption.
                if key in self._task_chain_registrations:
                    raise StoreConflict(
                        "task-chain registered but usage entry missing; fail-closed"
                    )
                raise StoreConflict(
                    "task-chain ledger entry missing for chain; fail-closed"
                )
            return self._task_chain_usage[key]

    def chain_settled_run_ids(self, scope: RunScope, task_chain_id: str) -> tuple[str, ...]:
        with self._lock:
            self._open()
            scope_key = encode(scope)
            return tuple(
                k[2] for k in self._task_chain_settled_runs
                if k[0] == scope_key and k[1] == task_chain_id
            )

    def chain_has_committed_runs(self, scope: RunScope, task_chain_id: str) -> bool:
        with self._lock:
            self._open()
            scope_key = encode(scope)
            for (run_key, record) in self._runs.items():
                if run_key[0] != scope_key:
                    continue
                snapshot = RunSnapshot.model_validate_json(record.snapshot)
                if snapshot.task_chain_id == task_chain_id and record.commits:
                    return True
            return False

    def _recompute_chain_totals(self, scope_key: str, chain_id: str) -> TaskChainUsage:
        """Recompute chain totals from settled-runs (sum, not append)."""
        model = tool = compaction = desc_model = desc_tool = notif_turns = 0
        elapsed = 0.0
        for key, rec in self._task_chain_settled_runs.items():
            if key[0] == scope_key and key[1] == chain_id:
                model += rec["model_calls"]
                tool += rec["tool_calls"]
                elapsed += rec["execution_elapsed_seconds"]
                compaction += rec["compaction_count"]
                desc_model += rec["descendant_model_calls"]
                desc_tool += rec["descendant_tool_calls"]
                notif_turns += rec["is_notification"]
        return TaskChainUsage(
            task_chain_id=chain_id, model_calls=model, tool_calls=tool,
            execution_elapsed_seconds=elapsed, compaction_count=compaction,
            descendant_model_calls=desc_model, descendant_tool_calls=desc_tool,
            notification_turns=notif_turns,
        )

    def _settle_run_in_lock(
        self, scope_key: str, committed: RunSnapshot, commit_id: str,
    ) -> None:
        """Per-run high-water mark: a run is charged once. If already settled,
        update commit_id only. Recompute chain totals from settled-runs."""
        update = self._compute_settlement_update(scope_key, committed, commit_id)
        if update is not None:
            self._apply_settlement_update(*update)

    def _compute_settlement_update(
        self, scope_key: str, committed: RunSnapshot, commit_id: str,
    ) -> tuple[tuple[str, str, str], dict, tuple[str, str], TaskChainUsage] | None:
        """Compute the settlement update WITHOUT mutating any state. Returns
        None if no chain, or a tuple of (run_key, run_record, chain_key, totals)
        ready for _apply_settlement_update. Raises on computation failure —
        before any committed data is mutated."""
        chain_id = committed.task_chain_id
        if not chain_id:
            return None
        run_id = committed.run_id
        rkey = (scope_key, chain_id, run_id)
        ch = committed.compaction_history
        compaction_count = (
            int(ch.get("prior_compactions", 0)) + int(ch.get("count", 0))
            if isinstance(ch, dict) else 0
        )
        if not isinstance(compaction_count, int) or compaction_count < 0:
            raise StoreConflict("invalid compaction_count in settlement")
        is_notification = bool(
            isinstance(committed.origin, dict) and committed.origin.get("notification")
        )
        existing = self._task_chain_settled_runs.get(rkey)
        if existing is not None:
            # Already settled — update commit_id AND usage to high-water mark.
            # The later terminal commit may have higher model_calls/tool_calls.
            run_record = {
                "commit_id": commit_id,
                "model_calls": max(existing["model_calls"], committed.model_calls),
                "tool_calls": max(existing["tool_calls"], committed.tool_calls),
                "execution_elapsed_seconds": max(
                    existing["execution_elapsed_seconds"],
                    committed.execution_elapsed_seconds,
                ),
                "compaction_count": max(existing["compaction_count"], compaction_count),
                "descendant_model_calls": max(
                    existing["descendant_model_calls"],
                    committed.descendant_model_calls,
                ),
                "descendant_tool_calls": max(
                    existing["descendant_tool_calls"],
                    committed.descendant_tool_calls,
                ),
                "is_notification": existing["is_notification"] or (1 if is_notification else 0),
            }
        else:
            run_record = {
                "commit_id": commit_id,
                "model_calls": committed.model_calls,
                "tool_calls": committed.tool_calls,
                "execution_elapsed_seconds": committed.execution_elapsed_seconds,
                "compaction_count": compaction_count,
                "descendant_model_calls": committed.descendant_model_calls,
                "descendant_tool_calls": committed.descendant_tool_calls,
                "is_notification": 1 if is_notification else 0,
            }
        # Compute totals by simulating the update on a copy.
        temp_settled = dict(self._task_chain_settled_runs)
        temp_settled[rkey] = run_record
        model = tool = compaction = desc_model = desc_tool = notif_turns = 0
        elapsed = 0.0
        for key, rec in temp_settled.items():
            if key[0] == scope_key and key[1] == chain_id:
                model += rec["model_calls"]
                tool += rec["tool_calls"]
                elapsed += rec["execution_elapsed_seconds"]
                compaction += rec["compaction_count"]
                desc_model += rec["descendant_model_calls"]
                desc_tool += rec["descendant_tool_calls"]
                notif_turns += rec["is_notification"]
        totals = TaskChainUsage(
            task_chain_id=chain_id, model_calls=model, tool_calls=tool,
            execution_elapsed_seconds=elapsed, compaction_count=compaction,
            descendant_model_calls=desc_model, descendant_tool_calls=desc_tool,
            notification_turns=notif_turns,
        )
        return (rkey, run_record, (scope_key, chain_id), totals)

    def _apply_settlement_update(
        self, rkey: tuple[str, str, str], run_record: dict,
        chain_key: tuple[str, str], totals: TaskChainUsage,
    ) -> None:
        """Apply a pre-computed settlement update. Pure dict assignment —
        called only after _compute_settlement_update succeeded and all
        committed state mutations have been applied."""
        self._task_chain_settled_runs[rkey] = run_record
        self._task_chain_usage[chain_key] = totals

    def settle_task_chain_usage(
        self, scope: RunScope, task_chain_id: str, *,
        commit_id: str, snapshot: RunSnapshot, notification_turn: bool = False,
        compaction_count: int = 0,
    ) -> TaskChainUsage:
        """External settlement — restricted. The snapshot must belong to this
        scope and the run must exist in the store. The atomic in-commit path
        (_settle_run_in_lock) is the primary entry."""
        if not isinstance(commit_id, str) or not commit_id:
            raise StoreConflict("settle requires a non-empty commit_id")
        check_scope(scope, snapshot)
        if snapshot.task_chain_id != task_chain_id:
            raise StoreConflict("settle task_chain_id does not match snapshot")
        # Reject non-terminal snapshots: only terminal runs can be settled.
        if not snapshot.phase.terminal:
            raise StoreConflict("settle requires a terminal snapshot")
        scope_key = encode(scope)
        with self._lock:
            self._open()
            # Verify the run exists in this store (not a foreign run).
            self._record(scope, snapshot.run_id)
            # Verify the commit_id was actually committed AND the snapshot matches
            # the canonical committed snapshot — not fabricated or tampered.
            record = self._runs[(encode(scope), snapshot.run_id)]
            if commit_id not in record.commits:
                raise StoreConflict("settle commit_id was not committed for this run")
            committed_snapshot = RunSnapshot.model_validate_json(
                record.snapshot_archive.restore(record.commits[commit_id].snapshot_ref)
            )
            # The CANONICAL committed snapshot must be terminal — a caller can
            # forge the passed snapshot's phase to "completed" while the actual
            # committed snapshot at that commit_id is non-terminal (e.g. an
            # early PREPARING_CONTEXT commit). Settling a non-terminal canonical
            # commit would let a reconciler refund the chain ledger. Reject:
            # only a canonical terminal commit may be settled. Mirrors the SQLite
            # external settle. (Runs are immutable once terminal.)
            if not committed_snapshot.phase.terminal:
                raise StoreConflict(
                    "settle commit is not a canonical terminal snapshot"
                )
            if (committed_snapshot.model_calls != snapshot.model_calls
                    or committed_snapshot.tool_calls != snapshot.tool_calls
                    or (committed_snapshot.execution_elapsed_seconds
                        != snapshot.execution_elapsed_seconds)
                    or committed_snapshot.descendant_model_calls != snapshot.descendant_model_calls
                    or committed_snapshot.descendant_tool_calls != snapshot.descendant_tool_calls
                    or committed_snapshot.task_chain_id != snapshot.task_chain_id):
                raise StoreConflict("settle snapshot does not match committed snapshot")
            # Derive compaction_count and is_notification from the CANONICAL
            # committed snapshot (history/origin), not the caller's parameters.
            # The caller's compaction_count/notification_turn are reconciler hints;
            # if they disagree with the canonical facts, reject (fail-closed) —
            # never silently trust a caller-supplied counter that could inflate
            # the chain ledger (e.g. compaction_count=999 on a run with no
            # compaction history, or notification_turn=False on a notification
            # run). This matches the SQLite external settle and the in-commit
            # settlement derivation. Settle the canonical committed snapshot
            # (not the caller's possibly-tampered copy) so a forged
            # compaction_history/origin on the caller snapshot cannot change the
            # ledger.
            canonical_ch = committed_snapshot.compaction_history
            canonical_compaction = (
                int(canonical_ch.get("prior_compactions", 0)) + int(canonical_ch.get("count", 0))
                if isinstance(canonical_ch, dict) else 0
            )
            canonical_notification = bool(
                isinstance(committed_snapshot.origin, dict)
                and committed_snapshot.origin.get("notification")
            )
            if compaction_count != canonical_compaction:
                raise StoreConflict(
                    "settle compaction_count disagrees with canonical compaction history"
                )
            if notification_turn != canonical_notification:
                raise StoreConflict(
                    "settle notification_turn disagrees with canonical run origin"
                )
            self._settle_run_in_lock(scope_key, committed_snapshot, commit_id)
            return self._task_chain_usage.get(
                (scope_key, task_chain_id),
                TaskChainUsage(task_chain_id=task_chain_id),
            )

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
            committed_preview = RunSnapshot.model_validate_json(saved)
            # Pre-compute the settlement update BEFORE mutating any committed
            # state. If settlement computation fails (bad ledger, compaction
            # conversion), no committed data has been touched — the Memory store
            # has no automatic rollback, so validation must precede mutation.
            settlement_update = None
            if committed_preview.phase.terminal and committed_preview.task_chain_id:
                settlement_update = self._compute_settlement_update(
                    encode(scope), committed_preview, batch.commit_id
                )
            # All validation passed; now mutate committed state.
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
            # Apply the pre-computed settlement (pure dict assignment, cannot fail).
            if settlement_update is not None:
                self._apply_settlement_update(*settlement_update)
            return committed_preview

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
