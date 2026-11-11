"""Account active execution in the ledger; stop before another expired effect."""

from __future__ import annotations

import hashlib
from time import monotonic

from ksadk.harness.stores.native_contract import RunStore

from .model_recovery_contract import RunClockQuote as RunClockQuote
from .model_recovery_contract import unavailable_uncertain_usage
from .state import CommitBatch, EffectReceipt, RunPhase, RunScope, RunSnapshot
from .tool_interruption import record_tool_interruption
from .transitions import commit_transition, finish


class ActiveRunClock:
    """Local monotonic intervals advance a persisted consumed-time counter.

    Host ports own cleanup. A lost running intent remains uncertain after
    attach; this clock does not claim to reconstruct unrecorded crash time.

    Elapsed time is always tracked — even for unbounded runs (no wall limit) —
    so chain accounting records real consumption. The limit only governs the
    ``remaining`` stop check; it does not gate time accumulation. Paused and
    approval-waiting phases do not accrue time.
    """

    def __init__(self, limit: float | None) -> None:
        self.limit = limit
        self.anchors: dict[str, float] = {}

    @staticmethod
    def active(snapshot: RunSnapshot) -> bool:
        return not snapshot.phase.terminal and snapshot.phase not in {
            RunPhase.PAUSED, RunPhase.AWAITING_INTERACTION,
        }

    def attach(self, snapshot: RunSnapshot) -> None:
        if self.active(snapshot):
            self.anchors.setdefault(snapshot.run_id, monotonic())

    def release(self, run_id: str) -> None:
        self.anchors.pop(run_id, None)

    def elapsed(self, snapshot: RunSnapshot, now: float | None = None) -> float:
        anchor = self.anchors.get(snapshot.run_id)
        now = monotonic() if now is None else now
        return snapshot.execution_elapsed_seconds + (
            max(0, now - anchor) if anchor is not None else 0
        )

    def quote(self, snapshot: RunSnapshot, *, wall_time: float,
              clock_domain_digest: str) -> RunClockQuote:
        now = monotonic()
        return RunClockQuote(
            source_snapshot_digest=hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest(),
            measured_elapsed_seconds=self.elapsed(snapshot, now),
            monotonic_seconds=now, wall_time=float(wall_time),
            clock_domain_digest=clock_domain_digest,
        )

    def remaining(self, snapshot: RunSnapshot) -> float | None:
        return (
            None if self.limit is None else
            self.limit - self.elapsed(snapshot) - snapshot.model_recovery_debt_seconds
        )

    def prepare_commit(
        self, previous: RunSnapshot, batch: CommitBatch,
    ) -> tuple[CommitBatch, float]:
        now = monotonic()
        # Always record elapsed time, even for unbounded runs, so chain
        # accounting captures real consumption. The limit only governs
        # the stop check in ``remaining``, not whether time is tracked.
        batch = batch.model_copy(update={"snapshot": batch.snapshot.model_copy(update={
            "execution_elapsed_seconds": self.elapsed(previous, now),
        })})
        return batch, now

    def committed(self, snapshot: RunSnapshot, now: float) -> None:
        if self.active(snapshot):
            self.anchors[snapshot.run_id] = now
        else:
            self.release(snapshot.run_id)


def wall_budget_stop(
    snapshot: RunSnapshot, store: RunStore, scope: RunScope, *, elapsed: float,
) -> CommitBatch:
    """Retain an undispatched/unknown pending receipt before a budget terminal."""
    updated = finish(snapshot, "budget_exhausted", "wall_time_hard_limit")
    receipts, extra = (), ()
    intent, receipt = None, None
    if snapshot.pending_intent:
        intent = store.get_intent(scope, snapshot.run_id, snapshot.pending_intent)
        existing = store.get_receipt(scope, snapshot.run_id, snapshot.pending_intent)
        receipt = existing
        if intent is not None and existing is None:
            running = intent.status == "running"
            if running:
                # The deadline proves elapsed time, not what a lost effect did.
                updated = finish(snapshot, "unverified", "effect_outcome_unknown")
            receipt = EffectReceipt(
                operation_key=intent.operation_key,
                status="uncertain" if running else "failed",
                result={"reason": "effect_outcome_unknown" if running else "not_dispatched",
                        "deadline_cause": "wall_time_hard_limit"},
            )
            receipts = (receipt,)
            extra = (("effect.received", {"operation_key": intent.operation_key,
                      "kind": intent.kind, "status": receipt.status}),)
            if running and intent.kind in {"model", "tool"}:
                counter = "model_calls" if intent.kind == "model" else "tool_calls"
                updated = updated.model_copy(update={
                    counter: getattr(snapshot, counter) + intent.loop_attempts,
                })
            if running and intent.kind == "model":
                extra = (*extra, unavailable_uncertain_usage(intent.operation_key))
    batch = commit_transition(
        snapshot, updated, "budget.exhausted",
        {"reason": "wall_time_hard_limit", "execution_elapsed_seconds": elapsed,
         **({"model_recovery_debt_seconds": snapshot.model_recovery_debt_seconds}
            if snapshot.model_recovery_debt_seconds else {})},
        receipts=receipts, extra_events=extra,
    )
    return record_tool_interruption(snapshot, batch, intent, receipt)
