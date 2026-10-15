"""Bounded PostgreSQL history queries and pagination."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any, Optional

from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE, KSADK_PG_SESSIONS_TABLE
from ksadk.sessions.base import Session, SessionEvent, SessionEventQuery
from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET


class _HistoryEventWindow(list[SessionEvent]):
    """Internal short-page proof: a byte-limited page may still have more rows."""

    def __init__(self, events, *, source_exhausted: bool):
        super().__init__(events)
        self.source_exhausted = source_exhausted


class _PostgresHistoryReadMixin:
    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        # seq 过滤先应用,再对结果集应用"最新 N 条" offset/limit 语义。
        conditions = ["namespace = $1", "session_id = $2"]
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._connection() as connection:
            if limit is not None:
                params.extend([limit, offset or 0])
                limit_param = len(params) - 1
                offset_param = len(params)
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_PG_EVENTS_TABLE}
                        WHERE {where_clause}
                        ORDER BY seq_id DESC
                        LIMIT ${limit_param} OFFSET ${offset_param}
                    ) AS latest_events
                    ORDER BY seq_id ASC
                """
            elif offset is not None:
                params.append(offset)
                offset_param = len(params)
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_PG_EVENTS_TABLE}
                        WHERE {where_clause}
                        ORDER BY seq_id DESC
                        OFFSET ${offset_param}
                    ) AS latest_events
                    ORDER BY seq_id ASC
                """
            else:
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE {where_clause}
                    ORDER BY seq_id ASC
                """
            bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
            async with (
                connection.transaction(isolation="repeatable_read", readonly=True)
                if bounded
                else nullcontext()
            ):
                body_bytes = None
                if bounded:
                    candidate = (
                        f"SELECT content_json,state_delta_json,metadata_json "
                        f"FROM {KSADK_PG_EVENTS_TABLE} WHERE {where_clause} "
                        "ORDER BY seq_id DESC "
                    )
                    if limit is not None:
                        candidate += f"LIMIT ${limit_param} OFFSET ${offset_param}"
                    elif offset is not None:
                        candidate += f"OFFSET ${offset_param}"
                    body_bytes = await self._precheck_history_body(connection, candidate, params)
                if bounded:
                    rows = await connection.fetch_history_page(
                        query, *params, estimated_bytes=body_bytes
                    )
                else:
                    fetch = connection.fetch_replay if limit is None else connection.fetch
                    rows = await fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def get_message_event_window(
        self,
        session_id: str,
        *,
        limit: int,
        from_start: bool = False,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        byte_limit: int = 64 * 1024,
    ) -> list[SessionEvent]:
        """Initial message projection window, bounded by rows AND JSON bytes.

        This is deliberately separate from get_events/query_events: those APIs
        must retain their exact row-limit semantics. The caller completes the
        boundary invocation before returning any messages to the client.
        """
        if not 1 <= limit <= 256:
            raise ValueError("Message projection window must be between 1 and 256 events")
        if not 1 <= byte_limit <= 512 * 1024:
            raise ValueError("Message projection window must be between 1 and 512 KiB")
        if from_start and type(self).query_events is not _PostgresHistoryReadMixin.query_events:
            return await self.query_events(
                SessionEventQuery(
                    session_ids=[session_id],
                    from_start=True,
                    order_by_seq=True,
                    limit=limit,
                    after_seq_id=after_seq_id,
                    before_seq_id=before_seq_id,
                )
            )
        if not from_start and type(self).get_events is not _PostgresHistoryReadMixin.get_events:
            return await self.get_events(
                session_id,
                limit=limit,
                after_seq_id=after_seq_id,
                before_seq_id=before_seq_id,
            )
        await self._ensure_schema()
        conditions = ["namespace=$1", "session_id=$2"]
        params: list[Any] = [self.namespace, session_id]
        for value, operator in ((after_seq_id, ">"), (before_seq_id, "<")):
            if value is not None:
                params.append(value)
                conditions.append(f"seq_id {operator} ${len(params)}")
        params.append(limit)
        direction = "ASC" if from_start else "DESC"
        fields = (
            "id,session_id,author,event_type,content_json,timestamp,"
            "state_delta_json,seq_id,invocation_id,metadata_json"
        )
        budget = CURRENT_HISTORY_READ_BUDGET.get()
        limit_param = len(params)
        params.append(budget.remaining if budget is not None else 2**63 - 1)
        byte_param = len(params)
        # A single statement gives the byte check and returned rows the same
        # snapshot. PostgreSQL withholds all JSON bodies on overflow; only
        # bounded row metadata and the required byte count cross the socket.
        # This avoids BEGIN/preflight/fetch/COMMIT round trips on every page.
        returned_fields = ",".join(
            f"CASE WHEN required_bytes <= ${byte_param}::bigint THEN {field} ELSE NULL END AS {field}"
            if field in {"content_json", "state_delta_json", "metadata_json"}
            else field
            for field in fields.split(",")
        )
        # Include variable-length identity columns and a conservative allowance
        # for numeric values, DataRow framing and the returned guard columns.
        # Crossing a byte ceiling with one additional large tool event nearly
        # doubles a transfer and synchronizes slow responses under load. Only
        # the first event may exceed the window, so an indivisible event still
        # makes progress (subject to the unchanged total request byte budget).
        wire_overhead = (
            " + ".join(
                f"COALESCE(octet_length({field}::text),0)"
                for field in ("id", "session_id", "author", "event_type", "invocation_id")
            )
            + " + 128"
        )
        query = f"""
            WITH bounded AS (
                SELECT {fields},
                    COALESCE(octet_length(content_json::text),0) +
                    COALESCE(octet_length(state_delta_json::text),0) +
                    COALESCE(octet_length(metadata_json::text),0) AS body_bytes,
                    {wire_overhead} AS wire_overhead
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE {" AND ".join(conditions)}
                ORDER BY seq_id {direction} LIMIT ${limit_param}
            ), measured AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY seq_id {direction}) AS position,
                    SUM(body_bytes + wire_overhead) OVER (
                    ORDER BY seq_id {direction} ROWS UNBOUNDED PRECEDING
                ) AS cumulative_bytes FROM bounded
            ), candidates AS (
                SELECT * FROM measured
                WHERE cumulative_bytes <= {byte_limit} OR position = 1
            ), checked AS (
                SELECT *, SUM(body_bytes + 1024) OVER () AS required_bytes,
                    ((SELECT COUNT(*) FROM bounded) < {limit}
                     AND COUNT(*) OVER () = (SELECT COUNT(*) FROM bounded)) AS source_exhausted
                FROM candidates
            )
            SELECT {returned_fields}, source_exhausted, required_bytes FROM checked
            ORDER BY seq_id ASC
        """
        # Small individual windows still create a large burst when many
        # projections fetch them simultaneously. Only the bounded non-Kernel
        # projection scope shares this gate; metadata, writes, replay and
        # ordinary/Kernel reads retain their existing concurrency. Waiting is
        # inside the projection's original deadline and never retries a query.
        shared = self._history_read_lease.get()
        gate = (
            self._history_transfer_slots
            if shared is not None and shared[0] is asyncio.current_task()
            else nullcontext()
        )
        async with gate:
            async with self._connection() as connection:
                rows = await connection.fetch(query, *params)
        if budget is not None and rows:
            budget.account(int(rows[0]["required_bytes"]))
        return _HistoryEventWindow(
            [self._event_from_row(row) for row in rows],
            source_exhausted=bool(rows[0]["source_exhausted"]) if rows else True,
        )

    async def get_session_summary_events(self, session_id: str) -> list[SessionEvent]:
        """Fetch prompt endpoints and the exact active/latest run summary.

        PostgreSQL ranks lifecycle facts by invocation without sending their JSON
        bodies to Python. A terminal run cannot hide an older active run after
        more than one page of lifecycle events.
        """
        queries = [
            SessionEventQuery(
                session_ids=[session_id],
                event_types=["user_message"],
                limit=1,
                from_start=True,
                order_by_seq=True,
            ),
            SessionEventQuery(
                session_ids=[session_id], event_types=["user_message"], limit=1, order_by_seq=True
            ),
        ]
        selected: dict[str, SessionEvent] = {}
        for query in queries:
            selected.update({event.id: event for event in await self.query_events(query)})
        run_id = "COALESCE(NULLIF(metadata_json->>'run_id',''), NULLIF(metadata_json->>'invocation_id',''), NULLIF(invocation_id,''), '')"
        status = (
            "COALESCE(NULLIF(metadata_json->>'status',''), "
            "NULLIF(metadata_json->>'run_status',''), NULLIF(content_json->>'status',''), "
            "NULLIF(content_json->'payload'->>'status',''), "
            "CASE event_type WHEN 'run.started' THEN 'in_progress' "
            "WHEN 'run.progress' THEN 'in_progress' "
            "WHEN 'run.interrupted' THEN 'interrupted' "
            "WHEN 'run.completed' THEN 'completed' "
            "WHEN 'run.failed' THEN 'failed' "
            "WHEN 'run.canceled' THEN 'cancelled' ELSE '' END)"
        )
        await self._ensure_schema()
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""WITH facts AS (
                    SELECT id, seq_id, timestamp, event_type, {run_id} AS run_id,
                           {status} AS status,
                           COALESCE(metadata_json->>'run_mode','unknown') AS run_mode,
                           COALESCE(metadata_json->>'run_trigger','unknown') AS run_trigger
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE namespace=$1 AND session_id=$2 AND event_type=ANY($3::text[])
                ), latest AS (
                    SELECT DISTINCT ON (run_id) * FROM facts
                    ORDER BY run_id, seq_id DESC
                ), chosen AS (
                    SELECT * FROM latest
                    ORDER BY CASE WHEN status=ANY($4::text[]) THEN 0
                                  WHEN status<>ALL($5::text[]) THEN 1 ELSE 2 END,
                             seq_id DESC LIMIT 1
                )
                SELECT chosen.*, classified.run_mode AS classified_mode,
                       classified.run_trigger AS classified_trigger
                FROM chosen LEFT JOIN LATERAL (
                    SELECT facts.run_mode, facts.run_trigger FROM facts
                    WHERE facts.run_id=chosen.run_id AND
                          (facts.run_mode<>'unknown' OR facts.run_trigger<>'unknown')
                    ORDER BY facts.seq_id DESC LIMIT 1
                ) classified ON true""",
                self.namespace,
                session_id,
                [
                    "run_status",
                    "run.started",
                    "run.progress",
                    "run.interrupted",
                    "run.completed",
                    "run.failed",
                    "run.canceled",
                ],
                ["in_progress", "running", "resuming", "starting"],
                [
                    "completed",
                    "failed",
                    "error",
                    "cancelled",
                    "canceled",
                    "aborted",
                    "interrupted",
                    "resume_failed",
                ],
            )
        if row is not None:
            selected[row["id"]] = SessionEvent(
                id=row["id"],
                session_id=session_id,
                event_type=row["event_type"],
                seq_id=int(row["seq_id"]),
                timestamp=float(row["timestamp"]),
                invocation_id=row["run_id"],
                content={"status": row["status"]},
                metadata={
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "run_mode": row["classified_mode"] or row["run_mode"],
                    "run_trigger": row["classified_trigger"] or row["run_trigger"],
                },
            )
        return sorted(selected.values(), key=lambda event: event.seq_id)

    async def get_max_seq_id(self, session_id: str) -> int:
        """Find a history snapshot boundary without fetching event JSON."""
        await self._ensure_schema()
        async with self._connection() as connection:
            return int(
                await connection.fetchval(
                    f"SELECT COALESCE(MAX(seq_id),0) FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2",
                    self.namespace,
                    session_id,
                )
                or 0
            )

    async def has_events(
        self,
        session_id: str,
        *,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
    ) -> bool:
        """Probe a page boundary without transferring a potentially huge body."""
        await self._ensure_schema()
        clauses = ["namespace=$1", "session_id=$2"]
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            clauses.append(f"seq_id>${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            clauses.append(f"seq_id<${len(params)}")
        async with self._connection() as connection:
            return bool(
                await connection.fetchval(
                    f"SELECT EXISTS(SELECT 1 FROM {KSADK_PG_EVENTS_TABLE} "
                    f"WHERE {' AND '.join(clauses)})",
                    *params,
                )
            )

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        await self._ensure_schema()
        conditions = ["namespace = $1", "session_id = $2"]
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._connection() as connection:
            query = f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE {where_clause}
            """
            return int(await connection.fetchval(query, *params) or 0)

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        if not session_ids:
            return []
        await self._ensure_schema()
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id,agent_id,user_id,title,title_source,summary,first_prompt,"
                f"last_prompt,state_json,created_at,updated_at,version "
                f"FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 "
                "AND id=ANY($2::text[])",
                self.namespace,
                session_ids,
            )
        by_id = {row["id"]: self._session_from_row(row, events=[]) for row in rows}
        return [by_id[item] for item in session_ids if item in by_id]

    async def get_session_ids_by_ids(
        self,
        session_ids: list[str],
        agent_id: str | None = None,
    ) -> list[str]:
        if not session_ids:
            return []
        await self._ensure_schema()
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 "
                "AND id=ANY($2::text[])" + (" AND agent_id=$3" if agent_id is not None else ""),
                self.namespace,
                session_ids,
                *([agent_id] if agent_id is not None else []),
            )
        found = {row["id"] for row in rows}
        return [sid for sid in session_ids if sid in found]

    async def list_session_ids(
        self,
        agent_id: str | None = None,
        user_id: str | None = None,
    ) -> list[str]:
        await self._ensure_schema()
        clauses = ["namespace=$1"]
        params: list[Any] = [self.namespace]
        for column, value in (("agent_id", agent_id), ("user_id", user_id)):
            if value is not None:
                params.append(value)
                clauses.append(f"{column}=${len(params)}")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE {' AND '.join(clauses)}",
                *params,
            )
        return [row["id"] for row in rows]

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        await self._ensure_schema()
        clauses = ["namespace=$1"]
        params: list[Any] = [self.namespace]
        if agent_id is not None:
            params.append(agent_id)
            clauses.append(f"agent_id=${len(params)}")
        if user_id is not None:
            params.append(user_id)
            clauses.append(f"user_id=${len(params)}")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id,agent_id,user_id,title,title_source,summary,first_prompt,"
                f"last_prompt,state_json,created_at,updated_at,version "
                f"FROM {KSADK_PG_SESSIONS_TABLE} WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC,created_at DESC,id DESC",
                *params,
            )
        return [self._session_from_row(row, events=[]) for row in rows]

    def _batch_event_where(self, query: SessionEventQuery) -> tuple[list[str], list[Any]]:
        clauses = ["event_row.namespace=$1"]
        params: list[Any] = [self.namespace]
        if query.session_ids is not None:
            if not query.session_ids:
                return ["FALSE"], []
            params.append(query.session_ids)
            clauses.append(f"event_row.session_id=ANY(${len(params)}::text[])")
        for value, clause in (
            (query.agent_id, "session_row.agent_id"),
            (query.after_seq_id, "event_row.seq_id >"),
            (query.before_seq_id, "event_row.seq_id <"),
            (query.invocation_id, "event_row.invocation_id"),
        ):
            if value is not None:
                params.append(value)
                operator = "" if clause.endswith((">", "<")) else "="
                clauses.append(f"{clause}{operator}${len(params)}")
        if query.event_types:
            params.append(query.event_types)
            clauses.append(f"event_row.event_type=ANY(${len(params)}::text[])")
        if query.run_id is not None:
            params.append(query.run_id)
            clauses.append(
                "(event_row.metadata_json->>'run_id'="
                f"${len(params)}"
                f" OR event_row.metadata_json->'tool_receipt'->>'run_id'=${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'run_id'=${len(params)}))"
            )
        if query.checkpoint_id is not None:
            params.append(query.checkpoint_id)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'="
                f"${len(params)}"
                f" OR event_row.metadata_json->'tool_receipt'->>'checkpoint_id'=${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'continuation_id'=${len(params)}))"
            )
        if query.checkpoint_ids:
            params.append(query.checkpoint_ids)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'=ANY("
                f"${len(params)}::text[])"
                " OR (event_row.event_type='continuation.created'"
                " AND event_row.content_json->'runtime_event'->>'continuation_id'=ANY("
                f"${len(params)}::text[])))"
            )
        if query.receipt_only:
            clauses.append(
                "event_row.event_type='tool_result' AND "
                "jsonb_typeof(event_row.metadata_json->'tool_receipt')='object' AND "
                "btrim(coalesce(nullif(event_row.metadata_json->'tool_receipt'->>'tool_name', ''), "
                "event_row.metadata_json->>'tool_name', ''))<>''"
            )
        return clauses, params

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(query)
        direction = "ASC" if query.from_start else "DESC"
        order = (
            f"event_row.session_id {direction},event_row.seq_id {direction},"
            f"event_row.id {direction}"
            if query.order_by_seq
            else f"event_row.timestamp {direction},event_row.session_id {direction},"
            f"event_row.seq_id {direction},event_row.id {direction}"
        )
        params.extend([query.limit, query.offset])
        async with self._connection() as connection:
            bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
            async with (
                connection.transaction(isolation="repeatable_read", readonly=True)
                if bounded
                else nullcontext()
            ):
                body_bytes = await self._precheck_history_body(
                    connection,
                    f"SELECT event_row.content_json,event_row.state_delta_json,"
                    f"event_row.metadata_json FROM {KSADK_PG_EVENTS_TABLE} event_row "
                    f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                    "ON session_row.namespace=event_row.namespace "
                    "AND session_row.id=event_row.session_id "
                    f"WHERE {' AND '.join(clauses)} ORDER BY {order} "
                    f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                    params,
                )
                fetch = connection.fetch_history_page if bounded else connection.fetch
                rows = await fetch(
                    f"SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
                    f"event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
                    f"event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
                    f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
                    f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                    "ON session_row.namespace=event_row.namespace "
                    "AND session_row.id=event_row.session_id "
                    f"WHERE {' AND '.join(clauses)} ORDER BY {order} "
                    f"LIMIT ${len(params) - 1} OFFSET ${len(params)}",
                    *params,
                    **({"estimated_bytes": body_bytes} if bounded else {}),
                )
        events = [self._event_from_row(row) for row in rows]
        if not query.from_start:
            events.reverse()
        return events

    async def count_event_query(self, query: SessionEventQuery) -> int:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(query)
        async with self._connection() as connection:
            return int(
                await connection.fetchval(
                    f"SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} event_row "
                    f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                    "ON session_row.namespace=event_row.namespace "
                    "AND session_row.id=event_row.session_id "
                    f"WHERE {' AND '.join(clauses)}",
                    *params,
                )
                or 0
            )

    def _agent_events_query_parts(
        self,
        agent_id: str,
        user_id: Optional[str],
    ) -> tuple[str, list[Any]]:
        """跨会话事件查询的公共 JOIN/WHERE。events 表无 agent_id 列，需 join sessions。"""
        conditions = [
            "e.namespace = $1",
            "s.namespace = e.namespace",
            "s.id = e.session_id",
            "s.agent_id = $2",
        ]
        params: list[Any] = [self.namespace, agent_id]
        if user_id is not None:
            params.append(user_id)
            conditions.append(f"s.user_id = ${len(params)}")
        where_clause = " AND ".join(conditions)
        from_clause = (
            f"FROM {KSADK_PG_EVENTS_TABLE} e "
            f"JOIN {KSADK_PG_SESSIONS_TABLE} s "
            "ON s.namespace = e.namespace AND s.id = e.session_id "
            f"WHERE {where_clause}"
        )
        return from_clause, params

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        from_clause, params = self._agent_events_query_parts(agent_id, user_id)
        columns = (
            "e.id, e.session_id, e.author, e.event_type, e.content_json, e.timestamp, "
            "e.state_delta_json, e.seq_id, e.invocation_id, e.metadata_json"
        )
        async with self._connection() as connection:
            if limit is not None or offset is not None:
                # 与 get_events 一致的"最新 N 条"尾部语义：先 DESC 取窗口再 ASC 重排
                if limit is not None:
                    params.append(limit)
                    limit_sql = f"LIMIT ${len(params)}"
                else:
                    limit_sql = ""
                params.append(offset or 0)
                offset_sql = f"OFFSET ${len(params)}"
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT {columns}
                        {from_clause}
                        ORDER BY e.timestamp DESC, e.seq_id DESC, e.id DESC
                        {limit_sql} {offset_sql}
                    ) AS latest_events
                    ORDER BY timestamp ASC, seq_id ASC, id ASC
                """
            else:
                query = f"""
                    SELECT {columns}
                    {from_clause}
                    ORDER BY e.timestamp ASC, e.seq_id ASC, e.id ASC
                """
            rows = await connection.fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        await self._ensure_schema()
        from_clause, params = self._agent_events_query_parts(agent_id, user_id)
        async with self._connection() as connection:
            query = f"SELECT COUNT(*) AS total {from_clause}"
            return int(await connection.fetchval(query, *params) or 0)
