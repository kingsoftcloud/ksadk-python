"""Replay a durable Native ledger into the canonical session event log.

The Native commit owns the source fact. The canonical session log is an
idempotent projection: each projected event has a deterministic identity, so
a crash after any individual write can replay the whole source event before
acknowledging its SQLite outbox cursor.
"""

from __future__ import annotations

from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.harness.native.state import RunScope
from ksadk.harness.native_projection import NativeEventProjection

from .native_contract import ScopeMismatch
from .native_sqlite import SQLiteRunStore


async def drain_native_outbox(
    store: SQLiteRunStore,
    canonical: RuntimeEventStore,
    *,
    scope: RunScope,
    run_id: str,
    session_id: str,
    consumer: str,
    limit: int = 100,
) -> int:
    """Persist up to ``limit`` Native facts, then acknowledge each source fact.

    The caller supplies a canonical store backed by durable session storage.
    Failure leaves the unacknowledged Native fact available for replay. Do not
    publish a live UI notification here: consumers replay the canonical log.
    """
    snapshot = store.load(scope, run_id)
    if snapshot.session_id != session_id:
        raise ScopeMismatch("run session identity mismatch")
    pending = store.pending_outbox(scope, run_id, consumer, limit=limit)
    if not pending:
        return 0

    projection = NativeEventProjection(scope, run_id)
    # Projection state includes drafts and usage accumulated before the first
    # unacknowledged event. Rebuild it from the source ledger on cold start.
    for historical in store.events(scope, run_id):
        if historical.seq >= pending[0].seq:
            break
        projection.consume(historical)

    for native in pending:
        for event in projection.consume(native):
            await canonical.persist_one(session_id, event)
        store.ack_outbox(
            scope, run_id, consumer, seq=native.seq, event_id=native.event_id
        )
    return len(pending)
