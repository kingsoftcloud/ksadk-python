"""Read and update local session state with the existing file lock."""

from __future__ import annotations

import json
import time
from typing import Optional

from ksadk.sessions._local_tables import KSADK_SESSIONS_TABLE, KSADK_STATES_TABLE
from ksadk.sessions.base import SessionState


class _LocalStateSyncMixin:
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
