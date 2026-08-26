"""LocalSessionService 的同步 SQLite 存储实现（纯移动自 local_service，行为不变）。

以 mixin 形式被 :class:`LocalSessionService` 继承，依赖宿主提供 ``_connection()``
上下文与模块级表名常量。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from ksadk.ids import new_session_id
from ksadk.sessions._local_tables import (
    KSADK_EVENTS_TABLE,
    KSADK_SESSIONS_TABLE,
    KSADK_STATES_TABLE,
)
from ksadk.sessions.base import Session, SessionEvent, SessionState, generate_id


class _LocalServiceSyncMixin:
    def _create_session_sync(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str],
    ) -> Session:
        with self._connection() as connection:
            if session_id:
                existing = self._get_session_sync(session_id, connection=connection)
                if existing is not None:
                    return existing

            now = time.time()
            session = Session(
                id=session_id or new_session_id(),
                agent_id=agent_id,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                f"""
                    INSERT INTO {KSADK_SESSIONS_TABLE} (
                        id, agent_id, user_id, title, title_source, summary,
                        first_prompt, last_prompt,
                        state_json, created_at, updated_at, version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    session.id,
                    session.agent_id,
                    session.user_id,
                    session.title,
                    session.title_source,
                    session.summary,
                    session.first_prompt,
                    session.last_prompt,
                    json.dumps(session.state),
                    session.created_at,
                    session.updated_at,
                    session.version,
                ),
            )
            connection.execute(
                f"""
                    INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                ("session", session.agent_id, session.user_id, session.id, "{}", 0, now),
            )
            connection.commit()
            return session

    def _get_session_sync(
        self,
        session_id: str,
        *,
        connection: Optional[sqlite3.Connection] = None,
        include_events: bool = True,
    ) -> Optional[Session]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                f"""
                    SELECT
                        id, agent_id, user_id, title, title_source, summary,
                        first_prompt, last_prompt,
                        state_json, created_at, updated_at, version
                    FROM {KSADK_SESSIONS_TABLE}
                    WHERE id = ?
                    """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return Session(
                id=row["id"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                title=row["title"],
                title_source=row["title_source"],
                summary=row["summary"],
                first_prompt=row["first_prompt"],
                last_prompt=row["last_prompt"],
                state=json.loads(row["state_json"] or "{}"),
                events=(
                    self._get_events_sync(session_id, connection=connection)
                    if include_events
                    else []
                ),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                version=row["version"],
            )
        finally:
            if owns_connection:
                connection.close()

    def _list_sessions_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        with self._connection() as connection:
            query = f"""
                    SELECT
                        id, agent_id, user_id, title, title_source, summary,
                        first_prompt, last_prompt,
                        state_json, created_at, updated_at, version
                    FROM {KSADK_SESSIONS_TABLE}
                    WHERE agent_id = ?
                """
            params: list[object] = [agent_id]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            query += " ORDER BY updated_at DESC, created_at DESC, id DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
                if offset is not None:
                    query += " OFFSET ?"
                    params.append(offset)
            elif offset is not None:
                query += " LIMIT -1 OFFSET ?"
                params.append(offset)
            rows = connection.execute(query, params).fetchall()
            return [
                Session(
                    id=row["id"],
                    agent_id=row["agent_id"],
                    user_id=row["user_id"],
                    title=row["title"],
                    title_source=row["title_source"],
                    summary=row["summary"],
                    first_prompt=row["first_prompt"],
                    last_prompt=row["last_prompt"],
                    state=json.loads(row["state_json"] or "{}"),
                    events=[],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    version=row["version"],
                )
                for row in rows
            ]

    def _count_sessions_sync(self, agent_id: str, user_id: Optional[str]) -> int:
        with self._connection() as connection:
            query = f"""
                    SELECT COUNT(*) AS total
                    FROM {KSADK_SESSIONS_TABLE}
                    WHERE agent_id = ?
                """
            params: list[object] = [agent_id]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            row = connection.execute(query, params).fetchone()
            return int(row["total"] if row else 0)

    def _delete_session_sync(self, session_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {KSADK_SESSIONS_TABLE} WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False

            connection.execute(
                f"DELETE FROM {KSADK_EVENTS_TABLE} WHERE session_id = ?", (session_id,)
            )
            connection.execute(
                f"DELETE FROM {KSADK_STATES_TABLE} WHERE session_id = ?", (session_id,)
            )
            connection.execute(f"DELETE FROM {KSADK_SESSIONS_TABLE} WHERE id = ?", (session_id,))
            connection.commit()
            return True

    def _append_event_sync(self, session_id: str, event: SessionEvent) -> SessionEvent:
        with self._connection() as connection:
            session_row = connection.execute(
                f"""
                    SELECT agent_id, user_id, state_json, version
                    FROM {KSADK_SESSIONS_TABLE}
                    WHERE id = ?
                    """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            next_seq = int(
                connection.execute(
                    f"SELECT COALESCE(MAX(seq_id), 0) + 1 "
                    f"FROM {KSADK_EVENTS_TABLE} WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            stored = SessionEvent(
                id=event.id or generate_id(),
                session_id=session_id,
                author=event.author,
                event_type=event.event_type,
                content=dict(event.content),
                timestamp=event.timestamp,
                state_delta=dict(event.state_delta),
                seq_id=next_seq,
                invocation_id=event.invocation_id,
                metadata=dict(event.metadata),
                seq_binding=event.seq_binding,
            )
            stored.bind_seq_id(next_seq)
            connection.execute(
                f"""
                    INSERT INTO {KSADK_EVENTS_TABLE} (
                        id, session_id, author, event_type, content_json, timestamp,
                        state_delta_json, seq_id, invocation_id, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    stored.id,
                    stored.session_id,
                    stored.author,
                    stored.event_type,
                    json.dumps(stored.content),
                    stored.timestamp,
                    json.dumps(stored.state_delta),
                    stored.seq_id,
                    stored.invocation_id,
                    json.dumps(stored.metadata),
                ),
            )

            updated_at = time.time()
            state = json.loads(session_row["state_json"] or "{}")
            version = int(session_row["version"] or 0)
            if stored.state_delta:
                state.update(stored.state_delta)
                version += 1

            connection.execute(
                f"""
                    UPDATE {KSADK_SESSIONS_TABLE}
                    SET state_json = ?, updated_at = ?, version = ?
                    WHERE id = ?
                    """,
                (json.dumps(state), updated_at, version, session_id),
            )

            if stored.state_delta:
                connection.execute(
                    f"""
                        INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                            scope, agent_id, user_id, session_id, state_json, version, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                    (
                        "session",
                        session_row["agent_id"],
                        session_row["user_id"],
                        session_id,
                        json.dumps(state),
                        version,
                        updated_at,
                    ),
                )

            connection.commit()
            return stored

    def _update_session_metadata_sync(
        self,
        session_id: str,
        title: Optional[str],
        title_source: Optional[str],
        summary: Optional[str],
        first_prompt: Optional[str],
        last_prompt: Optional[str],
    ) -> Session:
        with self._connection() as connection:
            row = connection.execute(
                f"""
                    SELECT
                        id, agent_id, user_id, title, title_source, summary,
                        first_prompt, last_prompt,
                        state_json, created_at, updated_at, version
                    FROM {KSADK_SESSIONS_TABLE}
                    WHERE id = ?
                    """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Session {session_id} not found")

            updated_at = time.time()
            next_title = row["title"] if title is None else title
            next_title_source = row["title_source"] if title_source is None else title_source
            next_summary = row["summary"] if summary is None else summary
            next_first_prompt = row["first_prompt"] if first_prompt is None else first_prompt
            next_last_prompt = row["last_prompt"] if last_prompt is None else last_prompt

            connection.execute(
                f"""
                    UPDATE {KSADK_SESSIONS_TABLE}
                    SET title = ?, title_source = ?, summary = ?, first_prompt = ?, last_prompt = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                (
                    next_title,
                    next_title_source,
                    next_summary,
                    next_first_prompt,
                    next_last_prompt,
                    updated_at,
                    session_id,
                ),
            )
            connection.commit()
            return Session(
                id=row["id"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                title=next_title,
                title_source=next_title_source,
                summary=next_summary,
                first_prompt=next_first_prompt,
                last_prompt=next_last_prompt,
                state=json.loads(row["state_json"] or "{}"),
                events=[],
                created_at=row["created_at"],
                updated_at=updated_at,
                version=row["version"],
            )

    def _get_events_sync(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> list[SessionEvent]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            # seq 过滤先应用,再对结果集应用"最新 N 条" offset/limit 语义。
            seq_clauses: list[str] = []
            seq_params: list[object] = []
            if after_seq_id is not None:
                seq_clauses.append("AND seq_id > ?")
                seq_params.append(after_seq_id)
            if before_seq_id is not None:
                seq_clauses.append("AND seq_id < ?")
                seq_params.append(before_seq_id)
            seq_clause = " ".join(seq_clauses)
            if limit is not None:
                query = f"""
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM (
                            SELECT id, session_id, author, event_type, content_json, timestamp,
                                   state_delta_json, seq_id, invocation_id, metadata_json
                            FROM {KSADK_EVENTS_TABLE}
                            WHERE session_id = ? {seq_clause}
                            ORDER BY seq_id DESC
                            LIMIT ? OFFSET ?
                        )
                        ORDER BY seq_id ASC
                    """
                params: list[object] = [session_id, *seq_params]
                params.extend([limit, offset or 0])
            elif offset is not None:
                query = f"""
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM (
                            SELECT id, session_id, author, event_type, content_json, timestamp,
                                   state_delta_json, seq_id, invocation_id, metadata_json
                            FROM {KSADK_EVENTS_TABLE}
                            WHERE session_id = ? {seq_clause}
                            ORDER BY seq_id DESC
                            LIMIT -1 OFFSET ?
                        )
                        ORDER BY seq_id ASC
                    """
                params = [session_id, *seq_params]
                params.append(offset)
            else:
                query = f"""
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_EVENTS_TABLE}
                        WHERE session_id = ? {seq_clause}
                        ORDER BY seq_id ASC
                    """
                params = [session_id, *seq_params]

            rows = connection.execute(query, params).fetchall()
            return [
                SessionEvent(
                    id=row["id"],
                    session_id=row["session_id"],
                    author=row["author"],
                    event_type=row["event_type"],
                    content=json.loads(row["content_json"] or "{}"),
                    timestamp=row["timestamp"],
                    state_delta=json.loads(row["state_delta_json"] or "{}"),
                    seq_id=row["seq_id"],
                    invocation_id=row["invocation_id"],
                    metadata=json.loads(row["metadata_json"] or "{}"),
                )
                for row in rows
            ]
        finally:
            if owns_connection:
                connection.close()

    def _get_event_by_id_sync(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        with self._connection() as connection:
            row = connection.execute(
                f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM {KSADK_EVENTS_TABLE}
                    WHERE session_id = ? AND id = ?
                    """,
                (session_id, event_id),
            ).fetchone()
            if row is None:
                return None
            return SessionEvent(
                id=row["id"],
                session_id=row["session_id"],
                author=row["author"],
                event_type=row["event_type"],
                content=json.loads(row["content_json"] or "{}"),
                timestamp=row["timestamp"],
                state_delta=json.loads(row["state_delta_json"] or "{}"),
                seq_id=row["seq_id"],
                invocation_id=row["invocation_id"],
                metadata=json.loads(row["metadata_json"] or "{}"),
            )

    def _get_events_by_invocation_id_sync(
        self,
        session_id: str,
        invocation_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        with self._connection() as connection:
            conditions = ["session_id = ?", "invocation_id = ?"]
            params: list[object] = [session_id, invocation_id]
            if after_seq_id is not None:
                conditions.append("seq_id > ?")
                params.append(after_seq_id)
            if before_seq_id is not None:
                conditions.append("seq_id < ?")
                params.append(before_seq_id)
            rows = connection.execute(
                f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM {KSADK_EVENTS_TABLE}
                    WHERE {" AND ".join(conditions)}
                    ORDER BY seq_id ASC
                    """,
                params,
            ).fetchall()
            return [
                SessionEvent(
                    id=row["id"],
                    session_id=row["session_id"],
                    author=row["author"],
                    event_type=row["event_type"],
                    content=json.loads(row["content_json"] or "{}"),
                    timestamp=row["timestamp"],
                    state_delta=json.loads(row["state_delta_json"] or "{}"),
                    seq_id=row["seq_id"],
                    invocation_id=row["invocation_id"],
                    metadata=json.loads(row["metadata_json"] or "{}"),
                )
                for row in rows
            ]

    def _count_events_sync(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        with self._connection() as connection:
            seq_clauses: list[str] = []
            params: list[object] = [session_id]
            if after_seq_id is not None:
                seq_clauses.append("AND seq_id > ?")
                params.append(after_seq_id)
            if before_seq_id is not None:
                seq_clauses.append("AND seq_id < ?")
                params.append(before_seq_id)
            seq_clause = " ".join(seq_clauses)
            row = connection.execute(
                f"""
                    SELECT COUNT(*) AS total
                    FROM {KSADK_EVENTS_TABLE}
                    WHERE session_id = ? {seq_clause}
                    """,
                params,
            ).fetchone()
            return int(row["total"] if row else 0)

    def _get_state_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
    ) -> Optional[SessionState]:
        with self._connection() as connection:
            if scope == "session" and session_id:
                session = self._get_session_sync(session_id, connection=connection)
                if session is None:
                    return None
                return SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=dict(session.state),
                    version=session.version,
                    updated_at=session.updated_at,
                )

            row = connection.execute(
                f"""
                    SELECT scope, agent_id, user_id, session_id, state_json, version, updated_at
                    FROM {KSADK_STATES_TABLE}
                    WHERE scope = ? AND agent_id = ? AND user_id = ? AND session_id = ?
                    """,
                (scope, agent_id, user_id or "", session_id or ""),
            ).fetchone()
            if row is None:
                return None

            return SessionState(
                scope=row["scope"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                session_id=row["session_id"],
                state=json.loads(row["state_json"] or "{}"),
                version=row["version"],
                updated_at=row["updated_at"],
            )

    def _update_state_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict,
    ) -> SessionState:
        with self._connection() as connection:
            updated_at = time.time()

            if scope == "session":
                if not session_id:
                    raise ValueError("session_id is required for session scope")
                session = self._get_session_sync(session_id, connection=connection)
                if session is None:
                    raise ValueError(f"Session {session_id} not found")

                next_state = dict(session.state)
                next_state.update(state_delta)
                next_version = session.version + 1
                connection.execute(
                    f"""
                        UPDATE {KSADK_SESSIONS_TABLE}
                        SET state_json = ?, updated_at = ?, version = ?
                        WHERE id = ?
                        """,
                    (json.dumps(next_state), updated_at, next_version, session_id),
                )
                connection.execute(
                    f"""
                        INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                            scope, agent_id, user_id, session_id, state_json, version, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                    (
                        "session",
                        session.agent_id,
                        session.user_id,
                        session.id,
                        json.dumps(next_state),
                        next_version,
                        updated_at,
                    ),
                )
                connection.commit()
                return SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=next_state,
                    version=next_version,
                    updated_at=updated_at,
                )

            row = connection.execute(
                f"""
                    SELECT state_json, version
                    FROM {KSADK_STATES_TABLE}
                    WHERE scope = ? AND agent_id = ? AND user_id = ? AND session_id = ?
                    """,
                (scope, agent_id, user_id or "", session_id or ""),
            ).fetchone()
            next_state = json.loads(row["state_json"] or "{}") if row else {}
            next_state.update(state_delta)
            next_version = (int(row["version"] or 0) + 1) if row else 1

            connection.execute(
                f"""
                    INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    scope,
                    agent_id,
                    user_id or "",
                    session_id or "",
                    json.dumps(next_state),
                    next_version,
                    updated_at,
                ),
            )
            connection.commit()
            return SessionState(
                scope=scope,
                agent_id=agent_id,
                user_id=user_id or "",
                session_id=session_id or "",
                state=next_state,
                version=next_version,
                updated_at=updated_at,
            )


__all__ = ["_LocalServiceSyncMixin"]
