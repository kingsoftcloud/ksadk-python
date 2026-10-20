"""Snapshot-bound PostgreSQL checkpoint reads and resume audits."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, nullcontext
from typing import Any

from ksadk.sessions._checkpoint_resume_audit import CheckpointResumeAudit
from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE, KSADK_PG_SESSIONS_TABLE
from ksadk.sessions.base import (
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    checkpoint_creation_identity,
    checkpoint_resume_identity,
)
from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET


class _PostgresCheckpointReadMixin:
    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        return await self._scan_checkpoint_events_with_connection(None, query)

    async def iter_resume_related_event_pages(
        self, session_id: str, *, page_size: int = 256,
        expected_session: Session | None = None,
    ) -> AsyncIterator[list[SessionEvent]]:
        """Scan only resume facts by sequence in one read-only snapshot."""
        await self._ensure_schema()
        kinds = ["run_checkpoint", "run_resume", "runtime_resume", "continuation.created", "continuation.resumed"]
        async with self._connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                if expected_session is not None:
                    from ksadk.sessions.errors import HistoryReloadRequired

                    owner = await connection.fetchrow(
                        f"SELECT agent_id,user_id,created_at FROM {KSADK_PG_SESSIONS_TABLE} "
                        "WHERE namespace=$1 AND id=$2",
                        self.namespace, session_id,
                    )
                    if (owner is None or owner["agent_id"] != expected_session.agent_id
                            or owner["user_id"] != expected_session.user_id
                            or float(owner["created_at"]) != expected_session.created_at):
                        raise HistoryReloadRequired("Checkpoint session changed during lookup")
                upper = int(await connection.fetchval(
                    f"SELECT COALESCE(MAX(seq_id),0) FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2",
                    self.namespace, session_id,
                ) or 0)
                count = int(await connection.fetchval(
                    f"SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2 AND event_type=ANY($3::text[]) "
                    "AND seq_id<=$4",
                    self.namespace, session_id, kinds, upper,
                ) or 0)
                if count > 8192:
                    raise HistoryProjectionBudgetExceeded(
                        "Checkpoint lookup exceeds 8192 related events"
                    )
                cursor = 0
                used = 0
                while True:
                    candidates = (
                        f"FROM {KSADK_PG_EVENTS_TABLE} WHERE namespace=$1 AND session_id=$2 "
                        "AND event_type=ANY($3::text[]) AND seq_id>$4 AND seq_id<=$5 "
                        "ORDER BY seq_id,id LIMIT $6"
                    )
                    args = (self.namespace, session_id, kinds, cursor, upper, page_size)
                    size = int(await connection.fetchval(
                        "SELECT COALESCE(SUM(octet_length(content_json::text)+"
                        "octet_length(state_delta_json::text)+"
                        "octet_length(metadata_json::text)+"
                        "octet_length(to_jsonb(id)::text)+"
                        "octet_length(to_jsonb(session_id)::text)+"
                        "octet_length(to_jsonb(author)::text)+"
                        "octet_length(to_jsonb(event_type)::text)+"
                        "COALESCE(octet_length(to_jsonb(invocation_id)::text),0)+256),0) "
                        "FROM (SELECT id,session_id,author,event_type,invocation_id,"
                        "content_json,state_delta_json,metadata_json "
                        + candidates + ") AS resume_candidates",
                        *args,
                    ) or 0)
                    if used + size > 16 * 1024 * 1024:
                        raise HistoryProjectionBudgetExceeded(
                            "Checkpoint lookup exceeds 16 MiB of related events"
                        )
                    rows = await connection.fetch(
                        "SELECT id,session_id,author,event_type,content_json,timestamp,"
                        "state_delta_json,seq_id,invocation_id,metadata_json "
                        + candidates, *args,
                    )
                    if not rows:
                        break
                    yield [self._event_from_row(row) for row in rows]
                    used += size
                    cursor = int(rows[-1]["seq_id"])
                    if len(rows) < page_size:
                        break

    async def _scan_checkpoint_events_with_connection(
        self,
        connection: Any | None,
        query: CheckpointEventQuery,
        cursor: tuple[float, str, int, str] | None = None,
    ) -> list[SessionEvent]:
        event_query = SessionEventQuery(
            session_ids=query.session_ids,
            agent_id=query.agent_id,
            event_types=["run_checkpoint", "continuation.created"],
            checkpoint_ids=query.checkpoint_ids,
            run_id=query.run_id,
            offset=query.offset,
            limit=query.limit,
            from_start=True,
        )
        if query.framework is None and connection is None:
            return await self.query_events(event_query)
        if connection is None:
            async with self._connection() as owned:
                bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
                async with (owned.transaction(isolation="repeatable_read", readonly=True)
                            if bounded else nullcontext()):
                    return await self._scan_checkpoint_events_with_connection(
                        owned, query, cursor,
                    )
        await self._ensure_schema()
        clauses, params = self._batch_event_where(event_query)
        if query.framework is not None:
            params.append(query.framework.lower())
            clauses.append(
                "(lower(event_row.metadata_json->>'framework')="
                f"${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                " AND lower(event_row.content_json->'runtime_event'->'source'->>'framework')="
                f"${len(params)}))"
            )
        if cursor is not None:
            placeholders = []
            for value in cursor:
                params.append(value)
                placeholders.append(f"${len(params)}")
            clauses.append(
                "(event_row.timestamp,event_row.session_id,event_row.seq_id,event_row.id) > "
                f"({','.join(placeholders)})"
            )
        params.extend([query.limit, query.offset])
        candidates = (
            f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
            f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
            "ON session_row.namespace=event_row.namespace "
            "AND session_row.id=event_row.session_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY event_row.timestamp,event_row.session_id,"
            f"event_row.seq_id,event_row.id LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        await self._precheck_history_body(
            connection,
            "SELECT event_row.content_json,event_row.state_delta_json,"
            "event_row.metadata_json " + candidates,
            params,
        )
        rows = await connection.fetch(
            "SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
            "event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
            "event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
            + candidates,
            *params,
        )
        return [self._event_from_row(row) for row in rows]

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        await self._ensure_schema()
        async with self._connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                token = self._checkpoint_snapshot_connection.set(connection)
                try:
                    cursor: tuple[float, str, int, str] | None = None
                    first_page = True
                    while True:
                        batch = await self._scan_checkpoint_events_with_connection(
                            connection,
                            CheckpointEventQuery(
                                **{
                                    **query.__dict__,
                                    "offset": query.offset if first_page else 0,
                                }
                            ),
                            cursor,
                        )
                        if not batch:
                            break
                        yield batch
                        if len(batch) < query.limit:
                            break
                        last = batch[-1]
                        cursor = (
                            last.timestamp,
                            last.session_id,
                            last.seq_id,
                            last.id,
                        )
                        first_page = False
                finally:
                    self._checkpoint_snapshot_connection.reset(token)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        candidate = None
        max_seq_id = 0
        audit = CheckpointResumeAudit()
        async with asyncio.timeout(self.db_budget.write_operation_seconds):
            async with aclosing(self.iter_resume_related_event_pages(session_id)) as pages:
                async for page in pages:
                    for event in page:
                        creation = checkpoint_creation_identity(event)
                        if event.event_type == "run_checkpoint":
                            metadata = event.metadata or {}
                            if str(metadata.get("run_id") or "") != run_id:
                                continue
                            max_seq_id = max(max_seq_id, event.seq_id)
                            if str(metadata.get("checkpoint_id") or "") == checkpoint_id:
                                candidate = event
                        elif creation is not None:
                            if creation == (run_id, checkpoint_id):
                                max_seq_id = max(max_seq_id, event.seq_id)
                                candidate = event
                        else:
                            identity = checkpoint_resume_identity(event)
                            if identity is not None and identity[:2] == (run_id, checkpoint_id):
                                audit.record(event, identity)
        return {"candidate": candidate, "max_seq_id": max_seq_id,
                "resume_count": audit["resume_count"],
                "last_resumed_at": audit["last_resumed_at"]}

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        from ksadk.sessions._postgres_checkpoint_stats import get_checkpoint_stats

        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        if not keys:
            return {"audits": {}, "latest_seq_ids": {}}
        await self._ensure_schema()
        active = self._checkpoint_snapshot_connection.get()
        async with asyncio.timeout(self.db_budget.write_operation_seconds):
            async with (self._connection() if active is None else nullcontext(active)) as connection:
                async with (connection.transaction(isolation="repeatable_read", readonly=True)
                            if active is None else nullcontext()):
                    # The master aggregation validates every known carrier before
                    # filtering identity. Bound its duplicated per-key JSON input
                    # in the same snapshot, preserving fail-loud audit semantics.
                    sessions = [key[0] for key in dict.fromkeys(keys)]
                    sizes = await connection.fetchrow(
                        f"SELECT COUNT(*) AS event_count, COALESCE(SUM("
                        "octet_length(event_row.content_json::text)+"
                        "octet_length(event_row.metadata_json::text)+1024),0) AS body_bytes "
                        f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
                        "JOIN unnest($2::text[]) requested(session_id) "
                        "ON event_row.session_id=requested.session_id "
                        "WHERE event_row.namespace=$1 AND event_row.event_type IN "
                        "('run_resume','runtime_resume','continuation.resumed','continuation.created')",
                        self.namespace, sessions,
                    )
                    size = int(sizes["body_bytes"])
                    if int(sizes["event_count"]) > 8192 or size > 16 * 1024 * 1024:
                        raise HistoryProjectionBudgetExceeded("Checkpoint audit exceeds related-event budget")
                    budget = CURRENT_HISTORY_READ_BUDGET.get()
                    if budget is not None:
                        budget.account(size)
                    token = self._checkpoint_snapshot_connection.set(connection)
                    try:
                        return await get_checkpoint_stats(self, keys)
                    finally:
                        self._checkpoint_snapshot_connection.reset(token)
