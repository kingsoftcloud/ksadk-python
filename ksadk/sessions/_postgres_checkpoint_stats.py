"""PostgreSQL checkpoint audit aggregation helpers."""

from __future__ import annotations

import json
import math
from typing import Any

from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE
from ksadk.sessions.base import SessionEvent, checkpoint_resume_identity


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
                   ) AS legacy_last_resumed_at,
                   ARRAY_AGG(event_row.timestamp ORDER BY event_row.id) FILTER (
                     WHERE event_row.event_type='continuation.resumed'
                   ) AS canonical_carrier_timestamps,
                   ARRAY_AGG(
                     COALESCE(
                       event_row.content_json->'runtime_event',
                       event_row.content_json->'session_event'->'payload'
                     )
                     ORDER BY event_row.id
                   ) FILTER (WHERE event_row.event_type='continuation.resumed')
                   AS canonical_payloads,
                   COUNT(event_row.id) FILTER (
                     WHERE event_row.event_type='run_resume'
                   ) AS legacy_resume_count
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
                   COALESCE(MAX(event_row.seq_id),0) AS latest_seq_id
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
        # Keep payload parsing outside SQL.  Canonical rows are
        # user-provided JSON and may carry malformed values (including
        # overflowed exponents); attempting a direct PostgreSQL float cast
        # would abort the whole stats query.  Legacy carrier timestamps
        # remain covered by the aggregate above.  Strict parsing here also
        # keeps backend stats fail-loud with REST projection for malformed
        # known canonical events.
        resume_count = int(row["legacy_resume_count"] or 0)
        last_resumed_at = row["legacy_last_resumed_at"]
        try:
            last_resumed_at = float(last_resumed_at)
        except (TypeError, ValueError):
            last_resumed_at = None
        if last_resumed_at is not None and not math.isfinite(last_resumed_at):
            last_resumed_at = None
        for carrier_timestamp, payload in zip(
            row["canonical_carrier_timestamps"] or (), row["canonical_payloads"] or ()
        ):
            # asyncpg returns jsonb as text unless a custom codec is installed.
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                continue
            identity = checkpoint_resume_identity(
                SessionEvent(
                    event_type="continuation.resumed",
                    content={"runtime_event": payload},
                    timestamp=carrier_timestamp,
                )
            )
            if identity is None:
                continue
            resume_count += 1
            candidate = identity[2]
            last_resumed_at = max(last_resumed_at or candidate, candidate)
        audits[(row["session_id"], row["run_id"], row["checkpoint_id"])] = {
            "resume_count": resume_count,
            "last_resumed_at": last_resumed_at,
        }
    for row in latest_rows:
        latest[(row["session_id"], row["run_id"])] = int(
            row["latest_seq_id"] or 0
        )
    return {"audits": audits, "latest_seq_ids": latest}
