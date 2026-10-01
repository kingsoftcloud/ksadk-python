"""PostgreSQL checkpoint audit aggregation helpers."""

from __future__ import annotations

import math
from typing import Any

from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE
from ksadk.sessions.base import (
    SessionEvent,
    checkpoint_creation_identity,
    checkpoint_resume_identity,
)


def _raw_session_event(session_id: str, raw_event: object) -> SessionEvent:
    if not isinstance(raw_event, dict):
        raise ValueError("canonical checkpoint carrier row is not an object")
    return SessionEvent(
        id=str(raw_event.get("id") or ""),
        session_id=session_id,
        event_type=str(raw_event.get("event_type") or ""),
        content=dict(raw_event.get("content") or {}),
        timestamp=raw_event.get("timestamp", 0.0),
        seq_id=int(raw_event.get("seq_id") or 0),
        invocation_id=raw_event.get("invocation_id"),
        metadata=dict(raw_event.get("metadata") or {}),
    )


async def get_checkpoint_stats(
    service: Any, keys: list[tuple[str, str, str]]
) -> dict[str, object]:
    if len(keys) > 50:
        raise ValueError("checkpoint stats batch cannot exceed 50 keys")
    unique_keys = list(dict.fromkeys(keys))
    audits = {
        key: {"resume_count": 0, "last_resumed_at": None} for key in unique_keys
    }
    run_keys = list(
        dict.fromkeys((session_id, run_id) for session_id, run_id, _ in unique_keys)
    )
    latest = {key: 0 for key in run_keys}
    if not unique_keys:
        return {"audits": audits, "latest_seq_ids": latest}
    await service._ensure_schema()
    connection = service._checkpoint_snapshot_connection.get()
    owns_connection = connection is None
    if owns_connection:
        connection = await service._pool.acquire()
    try:
        audit_rows = await connection.fetch(
            f"""WITH requested(session_id,run_id,checkpoint_id) AS (
                SELECT * FROM unnest($2::text[],$3::text[],$4::text[])
            )
            SELECT requested.session_id,requested.run_id,requested.checkpoint_id,
                   MAX(event_row.timestamp) FILTER (
                     WHERE event_row.event_type='run_resume'
                       AND event_row.timestamp > '-Infinity'::double precision
                       AND event_row.timestamp < 'Infinity'::double precision
                   ) AS legacy_last_resumed_at,
                   COUNT(event_row.id) FILTER (
                     WHERE event_row.event_type='run_resume'
                   ) AS legacy_resume_count,
                   ARRAY_AGG(event_row.timestamp ORDER BY event_row.id) FILTER (
                     WHERE event_row.event_type='continuation.resumed'
                   ) AS canonical_carrier_timestamps,
                   ARRAY_AGG(
                     COALESCE(
                       event_row.content_json->'runtime_event'->>'timestamp',
                       event_row.content_json->'session_event'->'payload'->>'timestamp'
                     )
                     ORDER BY event_row.id
                   ) FILTER (WHERE event_row.event_type='continuation.resumed')
                   AS canonical_timestamps,
                   (
                     SELECT COALESCE(
                       jsonb_agg(
                         jsonb_build_object(
                           'id', resume_row.id,
                           'event_type', resume_row.event_type,
                           'content', resume_row.content_json,
                           'timestamp', resume_row.timestamp,
                           'seq_id', resume_row.seq_id,
                           'invocation_id', resume_row.invocation_id,
                           'metadata', resume_row.metadata_json
                         ) ORDER BY resume_row.id
                       ),
                       '[]'::jsonb
                     )
                     FROM {KSADK_PG_EVENTS_TABLE} resume_row
                     WHERE resume_row.namespace=$1
                       AND resume_row.session_id=requested.session_id
                       AND resume_row.event_type='continuation.resumed'
                   ) AS canonical_resume_rows
            FROM requested
            LEFT JOIN {KSADK_PG_EVENTS_TABLE} event_row
              ON event_row.namespace=$1
             AND event_row.session_id=requested.session_id
             AND (
               (
                 event_row.event_type='run_resume'
                 AND event_row.metadata_json->>'run_id'=requested.run_id
                 AND event_row.metadata_json->>'checkpoint_id'=requested.checkpoint_id
               )
               OR (
                 event_row.event_type='continuation.resumed'
                 AND COALESCE(
                   event_row.metadata_json->>'run_id',
                   event_row.content_json->'runtime_event'->>'run_id',
                   event_row.content_json->'session_event'->'payload'->>'run_id'
                 )=requested.run_id
                 AND COALESCE(
                   event_row.content_json->'runtime_event'->>'continuation_id',
                   event_row.content_json->'session_event'->'payload'->>'continuation_id'
                 )=requested.checkpoint_id
                 AND COALESCE(
                   event_row.content_json->'runtime_event'->>'continuation_kind',
                   event_row.content_json->'session_event'->'payload'->>'continuation_kind'
                 )='graph_checkpoint'
               )
             )
            GROUP BY requested.session_id,requested.run_id,requested.checkpoint_id""",
            service.namespace,
            [key[0] for key in unique_keys],
            [key[1] for key in unique_keys],
            [key[2] for key in unique_keys],
        )
        latest_rows = await connection.fetch(
            f"""WITH requested(session_id,run_id) AS (
                SELECT * FROM unnest($2::text[],$3::text[])
            )
            SELECT requested.session_id,requested.run_id,
                   COALESCE(MAX(event_row.seq_id),0) AS latest_seq_id,
                   (
                     SELECT COALESCE(
                       jsonb_agg(
                         jsonb_build_object(
                           'id', creation_row.id,
                           'event_type', creation_row.event_type,
                           'content', creation_row.content_json,
                           'timestamp', creation_row.timestamp,
                           'seq_id', creation_row.seq_id,
                           'invocation_id', creation_row.invocation_id,
                           'metadata', creation_row.metadata_json
                         ) ORDER BY creation_row.id
                       ),
                       '[]'::jsonb
                     )
                     FROM {KSADK_PG_EVENTS_TABLE} creation_row
                     WHERE creation_row.namespace=$1
                       AND creation_row.session_id=requested.session_id
                       AND creation_row.event_type='continuation.created'
                   ) AS canonical_creation_rows
            FROM requested
            LEFT JOIN {KSADK_PG_EVENTS_TABLE} event_row
              ON event_row.namespace=$1
             AND event_row.session_id=requested.session_id
             AND (
               (
                 event_row.event_type='run_checkpoint'
                 AND event_row.metadata_json->>'run_id'=requested.run_id
               )
               OR (
                 event_row.event_type='continuation.created'
                 AND COALESCE(
                   event_row.metadata_json->>'run_id',
                   event_row.content_json->'runtime_event'->>'run_id',
                   event_row.content_json->'session_event'->'payload'->>'run_id'
                 )=requested.run_id
                 AND COALESCE(
                   event_row.content_json->'runtime_event'->>'continuation_kind',
                   event_row.content_json->'session_event'->'payload'->>'continuation_kind'
                 )='graph_checkpoint'
               )
             )
            GROUP BY requested.session_id,requested.run_id""",
            service.namespace,
            [key[0] for key in run_keys],
            [key[1] for key in run_keys],
        )
    finally:
        if owns_connection:
            await service._pool.release(connection)
    for row in audit_rows:
        # Validate every known canonical resume carrier in the session before
        # applying the identity filter above.  The SQL join intentionally
        # narrows counts to the requested checkpoint, but that must not hide a
        # corrupt known carrier from the same fail-loud policy used by REST,
        # in-memory, and SQLite paths.
        canonical_rows = row.get("canonical_resume_rows")
        resume_count = int(
            row.get("legacy_resume_count", row.get("resume_count", 0)) or 0
        )
        for raw_event in canonical_rows or ():
            event = _raw_session_event(str(row["session_id"]), raw_event)
            identity = checkpoint_resume_identity(event)
            if identity is None or identity[:2] != (
                str(row["run_id"]),
                str(row["checkpoint_id"]),
            ):
                continue
            resume_count += 1
        # Keep payload timestamp parsing outside SQL.  Canonical rows are
        # user-provided JSON and may carry malformed values (including
        # overflowed exponents); attempting a direct PostgreSQL float cast
        # would abort the whole stats query.  Legacy carrier timestamps
        # remain covered by the aggregate above.
        last_resumed_at = row["legacy_last_resumed_at"]
        try:
            last_resumed_at = float(last_resumed_at)
        except (TypeError, ValueError):
            last_resumed_at = None
        if last_resumed_at is not None and not math.isfinite(last_resumed_at):
            last_resumed_at = None
        # Older fake/compatibility rows may not expose canonical_resume_rows;
        # retain their payload-timestamp precedence fallback. Live PostgreSQL
        # always returns canonical_resume_rows from the correlated subquery.
        fallback_pairs = (
            zip(
                row.get("canonical_carrier_timestamps") or (),
                row.get("canonical_timestamps")
                or row.get("canonical_payloads")
                or (),
            )
            if canonical_rows is None
            else ()
        )
        for carrier_timestamp, raw_value in fallback_pairs:
            counted_identity = False
            # Compatibility fakes from the pre-integrity SQL shape return
            # canonical payload objects rather than extracted timestamps.
            # Reconstruct a complete carrier identity before validating it.
            if isinstance(raw_value, str):
                import json

                try:
                    raw_value = json.loads(raw_value)
                except (TypeError, ValueError):
                    raw_value = None
            if isinstance(raw_value, dict):
                from ksadk.events.canonical_store import canonical_storage_id

                payload = dict(raw_value)
                raw_timestamp = payload.get("timestamp")
                event = SessionEvent(
                    id=canonical_storage_id(
                        str(row["session_id"]), str(payload.get("event_id") or "")
                    ),
                    session_id=str(row["session_id"]),
                    event_type=str(payload.get("event_type") or ""),
                    content={"runtime_event": payload},
                    timestamp=carrier_timestamp,
                    seq_id=int(payload.get("seq") or 0),
                    metadata={"canonical_event_id": str(payload.get("event_id") or "")},
                )
                identity = checkpoint_resume_identity(event)
                if identity is None or identity[:2] != (
                    str(row["run_id"]),
                    str(row["checkpoint_id"]),
                ):
                    continue
                resume_count += 1
                counted_identity = True
                raw_timestamp = identity[2]
            else:
                raw_timestamp = raw_value
            try:
                carrier = float(carrier_timestamp)
            except (TypeError, ValueError):
                carrier = None
            if carrier is not None and not math.isfinite(carrier):
                carrier = None
            try:
                candidate = float(raw_timestamp)
            except (TypeError, ValueError):
                candidate = carrier
            if candidate is None or not math.isfinite(candidate):
                candidate = carrier
            if candidate is None:
                continue
            if not counted_identity:
                resume_count += 1
            last_resumed_at = max(last_resumed_at or candidate, candidate)
        audits[(row["session_id"], row["run_id"], row["checkpoint_id"])] = {
            "resume_count": resume_count,
            "last_resumed_at": last_resumed_at,
        }
    for row in latest_rows:
        for raw_event in row.get("canonical_creation_rows") or ():
            event = _raw_session_event(str(row["session_id"]), raw_event)
            checkpoint_creation_identity(event)
        latest[(row["session_id"], row["run_id"])] = int(
            row["latest_seq_id"] or 0
        )
    return {"audits": audits, "latest_seq_ids": latest}
