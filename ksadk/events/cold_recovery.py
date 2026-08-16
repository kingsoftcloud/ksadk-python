"""Cold recovery: deterministic outcomes for runs and items left open by a process exit.

The in-process path is :mod:`ksadk.events.pipeline` conformance recovery; this
module is its cold counterpart. Both produce the same kind of fact — canonical
events that enter the store and participate in replay — so consumers never
special-case a repaired stream. See ``docs/runtime-event-v2-cold-recovery-design.md``
for the decision rules.

Ownership: the outcome events use deterministic event ids
(:func:`ksadk.events.identity.stable_event_id` with ``framework="ksadk"``), so
two racing recoverers compute the same ids and the second writer is rejected by
``RuntimeEventStore._assert_same_fact``. Execution-level liveness (pod leases)
is out of scope here; the caller passes the ownership verdict in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ksadk.events.canonical import (
    ErrorInfo,
    ItemFailed,
    RunInterrupted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.canonical_replay import replay_projection
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.identity import stable_event_id
from ksadk.events.reducer import RunProjection

_RECOVERY_SOURCE_METADATA = {"recovery": "cold"}


@dataclass
class OpenItem:
    scope_id: str
    item_id: str
    item_kind: str


@dataclass
class RecoveryFinding:
    """One open run detected by the scan, with the facts needed to settle it."""

    run_id: str
    scope_id: str
    resumable: bool
    continuation_id: str | None
    open_items: list[OpenItem] = field(default_factory=list)
    last_seq: int = 0


@dataclass
class RecoveryReport:
    """Outcome of a cold-recovery pass over one session."""

    resumed_run_ids: list[str] = field(default_factory=list)
    interrupted_run_ids: list[str] = field(default_factory=list)
    written_events: list[RuntimeEvent] = field(default_factory=list)


def _recovery_source(scope_id: str) -> SourceRef:
    return SourceRef(
        framework="ksadk",
        metadata={**_RECOVERY_SOURCE_METADATA, "scope_id": scope_id},
    )


async def scan_open_runs(
    store: RuntimeEventStore,
    session_id: str,
) -> list[RecoveryFinding]:
    """Detect runs left open, with their resumability and open items."""

    findings: list[RecoveryFinding] = []
    run_ids = await store.list_run_ids(session_id)
    for run_id in run_ids:
        projection = await replay_projection(store, session_id, run_id=run_id)
        if projection.status not in (None, "running"):
            continue
        resumable = False
        continuation_id: str | None = None
        for continuation in projection.continuations:
            continuation_id = continuation.continuation_id
            if getattr(continuation, "resumable", False):
                resumable = True
        findings.append(
            RecoveryFinding(
                run_id=run_id,
                scope_id=_root_scope(projection),
                resumable=resumable,
                continuation_id=continuation_id,
                open_items=[
                    OpenItem(
                        scope_id=item.scope_id,
                        item_id=item.item_id,
                        item_kind=item.item_kind,
                    )
                    for item in projection.items
                    if item.status == "open"
                ],
                last_seq=projection.last_seq or 0,
            )
        )
    return findings


def _root_scope(projection: RunProjection) -> str:
    return f"run:{projection.run_id}" if projection.run_id else "session"


def _recovery_event_id(scope_id: str, item_id: str, kind: str, run_id: str) -> str:
    return stable_event_id(
        "ksadk",
        scope_id,
        item_id,
        kind,
        part_id="cold_recovery",
        native_occurrence_id=run_id,
        chunk_ordinal=0,
    )


def settle_finding(
    finding: RecoveryFinding,
    session_id: str,
    *,
    allow_resume: bool,
    timestamp: float,
    run_seq: int | None = None,
) -> list[RuntimeEvent]:
    """Synthesize the deterministic outcome events for one open run.

    ``allow_resume`` is the execution-level ownership verdict. When both the
    continuation facts say ``resumable`` and the caller allows takeover, no
    events are produced — the run is handed to the normal resume path.
    Otherwise every open item gets an outcome and the run is interrupted.
    """

    if finding.resumable and allow_resume:
        return []
    events: list[RuntimeEvent] = []
    base = dict(
        schema_version=2,
        timestamp=timestamp,
        run_id=finding.run_id,
        run_seq=run_seq,
        scope_id=finding.scope_id,
        source=_recovery_source(finding.scope_id),
    )
    # 结局事件占据 last_seq 之后的新 seq,reducer 要求 seq 严格单调。
    next_seq = finding.last_seq
    for item in finding.open_items:
        code = (
            "tool_outcome_unknown"
            if item.item_kind == "tool_call"
            else f"{item.item_kind}_outcome_unknown"
        )
        next_seq += 1
        events.append(
            ItemFailed(
                event_id=_recovery_event_id(
                    item.scope_id, item.item_id, "item.failed", finding.run_id
                ),
                seq=next_seq,
                item_id=item.item_id,
                item_kind=item.item_kind,
                error=ErrorInfo(
                    code=code,
                    message="process exited before the item settled",
                    source="cold_recovery",
                    scope_id=item.scope_id,
                    item_id=item.item_id,
                ),
                **{**base, "scope_id": item.scope_id},
            )
        )
    next_seq += 1
    events.append(
        RunInterrupted(
            event_id=_recovery_event_id(finding.scope_id, "run", "run.interrupted", finding.run_id),
            seq=next_seq,
            status="interrupted",
            reason="process_exit",
            continuation_id=finding.continuation_id,
            **base,
        )
    )
    return events


async def recover_session(
    store: RuntimeEventStore,
    session_id: str,
    *,
    allow_resume_for: "callable[[str], bool] | None" = None,
    timestamp: float = 0.0,
) -> RecoveryReport:
    """Scan a session and persist deterministic outcomes for orphaned runs.

    Written events reuse the pipeline's persistence idempotency: a second
    recoverer racing on the same session writes the same deterministic ids and
    is rejected as duplicate facts, not as an error.
    """

    report = RecoveryReport()
    for finding in await scan_open_runs(store, session_id):
        allow = allow_resume_for(finding.run_id) if allow_resume_for else False
        events = settle_finding(
            finding, session_id, allow_resume=allow, timestamp=timestamp
        )
        if not events:
            report.resumed_run_ids.append(finding.run_id)
            continue
        for event in events:
            persisted, _created = await store.persist_one(session_id, event)
            report.written_events.append(persisted)
        report.interrupted_run_ids.append(finding.run_id)
    return report


__all__ = [
    "OpenItem",
    "RecoveryFinding",
    "RecoveryReport",
    "recover_session",
    "scan_open_runs",
    "settle_finding",
]
