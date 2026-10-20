"""Transactional task-chain membership and monotonic committed usage accounting."""

from __future__ import annotations

import sqlite3

from ksadk.harness.native.state import RunScope, RunSnapshot

from .native_commit_history import decode_snapshot
from .native_contract import StoreConflict, TaskChainUsage, check_scope, encode


class SQLiteTaskChainLedger:
    """Transactional task-chain membership and monotonic committed usage accounting."""

    def _register_chain_in_txn(
        self,
        connection: sqlite3.Connection,
        scope_key: str,
        task_chain_id: str,
        run_id: str,
    ) -> None:
        """Register chain membership + zero-usage entry if new; verify if existing."""
        # Check if chain is already registered.
        reg_existing = connection.execute(
            "SELECT 1 FROM native_task_chain_registrations "
            "WHERE scope=? AND task_chain_id=? AND run_id=?",
            (scope_key, task_chain_id, run_id),
        ).fetchone()
        if reg_existing is not None:
            return  # already registered this run for this chain
        # Check if chain exists at all (any registration).
        chain_exists = connection.execute(
            "SELECT 1 FROM native_task_chain_registrations WHERE scope=? AND task_chain_id=?",
            (scope_key, task_chain_id),
        ).fetchone()
        if chain_exists is not None:
            # Existing chain: must have a usage entry. If missing → corruption.
            usage_exists = connection.execute(
                "SELECT 1 FROM native_task_chain_usage WHERE scope=? AND task_chain_id=?",
                (scope_key, task_chain_id),
            ).fetchone()
            if usage_exists is None:
                raise StoreConflict("task-chain registered but usage entry missing; fail-closed")
            # Add this run as a member.
            connection.execute(
                "INSERT INTO native_task_chain_registrations"
                "(scope, task_chain_id, run_id, registered_at) VALUES (?, ?, ?, ?)",
                (scope_key, task_chain_id, run_id, self._chain_timestamp()),
            )
        else:
            # No registration row for this (chain, run). Check if the chain has
            # a usage entry from a prior migration (legacy DB without registrations
            # table). If so, this is an existing chain that needs backfill, not
            # a brand-new chain.
            usage_exists = connection.execute(
                "SELECT 1 FROM native_task_chain_usage WHERE scope=? AND task_chain_id=?",
                (scope_key, task_chain_id),
            ).fetchone()
            if usage_exists is not None:
                # Legacy chain with usage but no registration: backfill membership.
                connection.execute(
                    "INSERT INTO native_task_chain_registrations"
                    "(scope, task_chain_id, run_id, registered_at) VALUES (?, ?, ?, ?)",
                    (scope_key, task_chain_id, run_id, self._chain_timestamp()),
                )
            else:
                # Truly new chain: create registration + zero usage.
                connection.execute(
                    "INSERT INTO native_task_chain_registrations"
                    "(scope, task_chain_id, run_id, registered_at) VALUES (?, ?, ?, ?)",
                    (scope_key, task_chain_id, run_id, self._chain_timestamp()),
                )
                connection.execute(
                    "INSERT INTO native_task_chain_usage(scope, task_chain_id, "
                    "model_calls, tool_calls, execution_elapsed_seconds, "
                    "compaction_count, descendant_model_calls, "
                    "descendant_tool_calls, notification_turns) "
                    "VALUES (?, ?, 0, 0, 0.0, 0, 0, 0, 0)",
                    (scope_key, task_chain_id),
                )

    def register_task_chain(
        self,
        scope: RunScope,
        task_chain_id: str,
        run_id: str,
    ) -> None:
        key = encode(scope)
        with self._transaction(write=True) as connection:
            self._register_chain_in_txn(connection, key, task_chain_id, run_id)

    def chain_is_registered(self, scope: RunScope, task_chain_id: str) -> bool:
        key = encode(scope)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM native_task_chain_registrations "
                "WHERE scope=? AND task_chain_id=? LIMIT 1",
                (key, task_chain_id),
            ).fetchone()
            return row is not None

    def task_chain_usage(self, scope: RunScope, task_chain_id: str) -> TaskChainUsage:
        key = encode(scope)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT model_calls, tool_calls, execution_elapsed_seconds, "
                "compaction_count, descendant_model_calls, descendant_tool_calls, "
                "notification_turns "
                "FROM native_task_chain_usage WHERE scope=? AND task_chain_id=?",
                (key, task_chain_id),
            ).fetchone()
            if row is None:
                # Check if chain is registered but usage missing → corruption.
                reg = connection.execute(
                    "SELECT 1 FROM native_task_chain_registrations "
                    "WHERE scope=? AND task_chain_id=? LIMIT 1",
                    (key, task_chain_id),
                ).fetchone()
                if reg is not None:
                    raise StoreConflict(
                        "task-chain registered but usage entry missing; fail-closed"
                    )
                raise StoreConflict("task-chain ledger entry missing for chain; fail-closed")
            return TaskChainUsage(
                task_chain_id=task_chain_id,
                model_calls=int(row[0]),
                tool_calls=int(row[1]),
                execution_elapsed_seconds=float(row[2]),
                compaction_count=int(row[3]),
                descendant_model_calls=int(row[4]),
                descendant_tool_calls=int(row[5]),
                notification_turns=int(row[6]),
            )

    def chain_settled_run_ids(self, scope: RunScope, task_chain_id: str) -> tuple[str, ...]:
        key = encode(scope)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT run_id FROM native_task_chain_settled_runs "
                "WHERE scope=? AND task_chain_id=?",
                (key, task_chain_id),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def chain_has_committed_runs(self, scope: RunScope, task_chain_id: str) -> bool:
        key = encode(scope)
        with self._transaction() as connection:
            # Any committed member pins the ledger, including a later member
            # when the first member was created but never committed.
            row = connection.execute(
                "SELECT 1 FROM native_runs AS r "
                "JOIN native_commits AS c ON c.scope=r.scope AND c.run_id=r.run_id "
                "WHERE r.scope=? AND json_extract(r.snapshot, '$.task_chain_id')=? LIMIT 1",
                (key, task_chain_id),
            ).fetchone()
            return row is not None

    def settle_task_chain_usage(
        self,
        scope: RunScope,
        task_chain_id: str,
        *,
        commit_id: str,
        snapshot: RunSnapshot,
        notification_turn: bool = False,
        compaction_count: int = 0,
    ) -> TaskChainUsage:
        """External settlement entry point — restricted. The snapshot must belong
        to this scope and the run must exist in the store. The atomic in-commit
        path (_settle_chain_in_transaction) is the primary entry; this method is
        for host-initiated reconciliation only and enforces the same scope/run
        checks as the RunStore contract.
        """
        if not isinstance(commit_id, str) or not commit_id:
            raise StoreConflict("settle requires a non-empty commit_id")
        check_scope(scope, snapshot)
        if snapshot.task_chain_id != task_chain_id:
            raise StoreConflict("settle task_chain_id does not match snapshot")
        # Reject non-terminal snapshots: only terminal runs can be settled.
        if not snapshot.phase.terminal:
            raise StoreConflict("settle requires a terminal snapshot")
        key = encode(scope)
        with self._transaction(write=True) as connection:
            # Verify the run exists in this store (not a foreign run).
            self._row(connection, key, snapshot.run_id)
            # Verify the commit_id was actually committed AND the snapshot matches
            # the canonical committed snapshot — not a fabricated commit or a
            # tampered snapshot with inflated model_calls.
            committed_snapshot = decode_snapshot(connection, key, snapshot.run_id, commit_id)
            # The CANONICAL committed snapshot must be terminal — a caller can
            # forge the passed snapshot's phase to "completed" while the actual
            # committed snapshot at that commit_id is non-terminal (e.g. an
            # early PREPARING_CONTEXT commit). Settling a non-terminal canonical
            # commit would let a reconciler refund the chain ledger (the
            # non-terminal commit carries lower usage). Reject: only a canonical
            # terminal commit may be settled. (Runs are immutable once terminal;
            # do not invent multiple mutable terminal commits.)
            if not committed_snapshot.phase.terminal:
                raise StoreConflict("settle commit is not a canonical terminal snapshot")
            # The usage counters are taken from the CANONICAL committed snapshot,
            # not the caller's (possibly-forged) copy. The caller's budget
            # fields were already checked to match the canonical snapshot above;
            # use the canonical values so a stale/forged caller snapshot cannot
            # refund or inflate the ledger.
            settle_model_calls = committed_snapshot.model_calls
            settle_tool_calls = committed_snapshot.tool_calls
            settle_elapsed = committed_snapshot.execution_elapsed_seconds
            settle_desc_model = committed_snapshot.descendant_model_calls
            settle_desc_tool = committed_snapshot.descendant_tool_calls
            if (
                settle_model_calls != snapshot.model_calls
                or settle_tool_calls != snapshot.tool_calls
                or settle_elapsed != snapshot.execution_elapsed_seconds
                or settle_desc_model != snapshot.descendant_model_calls
                or settle_desc_tool != snapshot.descendant_tool_calls
                or committed_snapshot.task_chain_id != snapshot.task_chain_id
            ):
                raise StoreConflict("settle snapshot does not match committed snapshot")
            # Derive compaction_count and is_notification from the CANONICAL
            # committed snapshot (history/origin), not the caller's parameters.
            # The caller's compaction_count/notification_turn are reconciler hints;
            # if they disagree with the canonical facts, reject (fail-closed) —
            # never silently trust a caller-supplied counter that could reset or
            # inflate the chain ledger (e.g. compaction_count=999 on a run with
            # no compaction history, or notification_turn=False on a notification
            # run). This matches the in-commit settlement derivation.
            canonical_compaction = 0
            ch = committed_snapshot.compaction_history
            if isinstance(ch, dict):
                canonical_compaction = int(ch.get("prior_compactions", 0)) + int(ch.get("count", 0))
            canonical_notification = bool(
                isinstance(committed_snapshot.origin, dict)
                and committed_snapshot.origin.get("notification")
            )
            if compaction_count != canonical_compaction:
                raise StoreConflict(
                    "settle compaction_count disagrees with canonical compaction history"
                )
            if notification_turn != canonical_notification:
                raise StoreConflict("settle notification_turn disagrees with canonical run origin")
            existing = connection.execute(
                "SELECT model_calls, tool_calls, execution_elapsed_seconds, "
                "compaction_count, descendant_model_calls, descendant_tool_calls, "
                "is_notification FROM native_task_chain_settled_runs "
                "WHERE scope=? AND task_chain_id=? AND run_id=?",
                (key, task_chain_id, snapshot.run_id),
            ).fetchone()
            if existing is not None:
                # Already settled — update commit_id AND usage. The usage is
                # MONOTONIC (high-water): each settled-run counter is the MAX of
                # the existing value and the canonical committed value, so a
                # re-settle with an earlier/lower commit (or a forged stale
                # snapshot) can never refund the chain ledger. is_notification is
                # latched (once a notification turn, always one).
                connection.execute(
                    "UPDATE native_task_chain_settled_runs SET commit_id=?, "
                    "model_calls=?, tool_calls=?, execution_elapsed_seconds=?, "
                    "compaction_count=?, descendant_model_calls=?, "
                    "descendant_tool_calls=?, is_notification=? "
                    "WHERE scope=? AND task_chain_id=? AND run_id=?",
                    (
                        commit_id,
                        max(existing[0], settle_model_calls),
                        max(existing[1], settle_tool_calls),
                        max(existing[2], settle_elapsed),
                        max(existing[3], canonical_compaction),
                        max(existing[4], settle_desc_model),
                        max(existing[5], settle_desc_tool),
                        existing[6] or (1 if canonical_notification else 0),
                        key,
                        task_chain_id,
                        snapshot.run_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO native_task_chain_settled_runs "
                    "(scope, task_chain_id, run_id, commit_id, model_calls, "
                    "tool_calls, execution_elapsed_seconds, compaction_count, "
                    "descendant_model_calls, descendant_tool_calls, "
                    "is_notification, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        task_chain_id,
                        snapshot.run_id,
                        commit_id,
                        settle_model_calls,
                        settle_tool_calls,
                        settle_elapsed,
                        canonical_compaction,
                        settle_desc_model,
                        settle_desc_tool,
                        1 if canonical_notification else 0,
                        self._chain_timestamp(),
                    ),
                )
            # Recompute totals from settled-runs.
            totals = connection.execute(
                "SELECT COALESCE(SUM(model_calls),0), COALESCE(SUM(tool_calls),0), "
                "COALESCE(SUM(execution_elapsed_seconds),0.0), "
                "COALESCE(SUM(compaction_count),0), "
                "COALESCE(SUM(descendant_model_calls),0), "
                "COALESCE(SUM(descendant_tool_calls),0), "
                "COALESCE(SUM(is_notification),0) "
                "FROM native_task_chain_settled_runs "
                "WHERE scope=? AND task_chain_id=?",
                (key, task_chain_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO native_task_chain_usage(scope, task_chain_id, "
                "model_calls, tool_calls, execution_elapsed_seconds, "
                "compaction_count, descendant_model_calls, descendant_tool_calls, "
                "notification_turns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope, task_chain_id) DO UPDATE SET "
                "model_calls=excluded.model_calls, tool_calls=excluded.tool_calls, "
                "execution_elapsed_seconds=excluded.execution_elapsed_seconds, "
                "compaction_count=excluded.compaction_count, "
                "descendant_model_calls=excluded.descendant_model_calls, "
                "descendant_tool_calls=excluded.descendant_tool_calls, "
                "notification_turns=excluded.notification_turns",
                (
                    key,
                    task_chain_id,
                    int(totals[0]),
                    int(totals[1]),
                    float(totals[2]),
                    int(totals[3]),
                    int(totals[4]),
                    int(totals[5]),
                    int(totals[6]),
                ),
            )
            return TaskChainUsage(
                task_chain_id=task_chain_id,
                model_calls=int(totals[0]),
                tool_calls=int(totals[1]),
                execution_elapsed_seconds=float(totals[2]),
                compaction_count=int(totals[3]),
                descendant_model_calls=int(totals[4]),
                descendant_tool_calls=int(totals[5]),
                notification_turns=int(totals[6]),
            )

    def _settle_chain_in_transaction(
        self,
        connection: sqlite3.Connection,
        scope_key: str,
        committed: RunSnapshot,
        commit_id: str,
    ) -> None:
        """Settle task-chain usage within the caller's commit transaction.

        Per-run high-water mark: a run is charged exactly once regardless of how
        many terminal commits it has (retry/crash-recovery). The settled-runs
        subtable keys on (scope, chain, run_id) — if this run already settled,
        skip. Chain totals are recomputed from the settled-runs subtable, so a
        re-settlement with a different commit_id for the same run updates the row
        (not duplicates it). A failure here rolls back the entire commit.
        """
        chain_id = committed.task_chain_id
        if not chain_id:
            return
        run_id = committed.run_id
        # Check if this run already settled for this chain.
        # Compaction count: total_compactions = prior_compactions + count.
        compaction_count = 0
        ch = committed.compaction_history
        if isinstance(ch, dict):
            compaction_count = int(ch.get("prior_compactions", 0)) + int(ch.get("count", 0))
        is_notification = bool(
            isinstance(committed.origin, dict) and committed.origin.get("notification")
        )
        # A later terminal commit can increase the contribution. Upsert a
        # high-water mark, then recompute totals, matching MemoryRunStore.
        connection.execute(
            "INSERT INTO native_task_chain_settled_runs "
            "(scope, task_chain_id, run_id, commit_id, model_calls, tool_calls, "
            "execution_elapsed_seconds, compaction_count, descendant_model_calls, "
            "descendant_tool_calls, is_notification, settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, task_chain_id, run_id) DO UPDATE SET "
            "commit_id=excluded.commit_id, "
            "model_calls=MAX(model_calls, excluded.model_calls), "
            "tool_calls=MAX(tool_calls, excluded.tool_calls), "
            "execution_elapsed_seconds=MAX(execution_elapsed_seconds, "
            "excluded.execution_elapsed_seconds), "
            "compaction_count=MAX(compaction_count, excluded.compaction_count), "
            "descendant_model_calls=MAX(descendant_model_calls, excluded.descendant_model_calls), "
            "descendant_tool_calls=MAX(descendant_tool_calls, excluded.descendant_tool_calls), "
            "is_notification=MAX(is_notification, excluded.is_notification)",
            (
                scope_key,
                chain_id,
                run_id,
                commit_id,
                committed.model_calls,
                committed.tool_calls,
                committed.execution_elapsed_seconds,
                compaction_count,
                committed.descendant_model_calls,
                committed.descendant_tool_calls,
                1 if is_notification else 0,
                self._chain_timestamp(),
            ),
        )
        # Recompute chain totals from the settled-runs subtable (sum, not append).
        totals = connection.execute(
            "SELECT "
            "COALESCE(SUM(model_calls), 0), COALESCE(SUM(tool_calls), 0), "
            "COALESCE(SUM(execution_elapsed_seconds), 0.0), "
            "COALESCE(SUM(compaction_count), 0), "
            "COALESCE(SUM(descendant_model_calls), 0), "
            "COALESCE(SUM(descendant_tool_calls), 0), "
            "COALESCE(SUM(is_notification), 0) "
            "FROM native_task_chain_settled_runs "
            "WHERE scope=? AND task_chain_id=?",
            (scope_key, chain_id),
        ).fetchone()
        connection.execute(
            "INSERT INTO native_task_chain_usage(scope, task_chain_id, model_calls, "
            "tool_calls, execution_elapsed_seconds, compaction_count, "
            "descendant_model_calls, descendant_tool_calls, notification_turns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, task_chain_id) DO UPDATE SET "
            "model_calls=excluded.model_calls, tool_calls=excluded.tool_calls, "
            "execution_elapsed_seconds=excluded.execution_elapsed_seconds, "
            "compaction_count=excluded.compaction_count, "
            "descendant_model_calls=excluded.descendant_model_calls, "
            "descendant_tool_calls=excluded.descendant_tool_calls, "
            "notification_turns=excluded.notification_turns",
            (
                scope_key,
                chain_id,
                int(totals[0]),
                int(totals[1]),
                float(totals[2]),
                int(totals[3]),
                int(totals[4]),
                int(totals[5]),
                int(totals[6]),
            ),
        )
