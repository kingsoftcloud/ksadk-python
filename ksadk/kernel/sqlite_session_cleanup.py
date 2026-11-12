"""Retire SQLite kernel work whose authoritative local session was deleted."""

import aiosqlite

from ksadk.sessions._local_tables import KSADK_SESSIONS_TABLE


async def install_wait_delete_trigger(connection: aiosqlite.Connection) -> None:
    await connection.execute(f"""
        CREATE TRIGGER IF NOT EXISTS kernel_cancel_deleted_session_waits
        AFTER DELETE ON {KSADK_SESSIONS_TABLE}
        BEGIN
            UPDATE mcp_authorization_waits SET status='cancelled' WHERE session_id=OLD.id
                AND status IN ('pending','resolving','unknown','consumed');
        END
    """)


async def _has_waits(connection: aiosqlite.Connection) -> bool:
    async with connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_authorization_waits'"
    ) as cursor:
        return await cursor.fetchone() is not None


def _retire_statements(scope: str, reason: str, run_fence: str) -> tuple[str, ...]:
    # All SQL fragments here are internal. Session ids are always
    # supplied by SQLite (OLD.id) or bound parameters.
    timestamp = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    return (
        f"""UPDATE kernel_runs SET state='interrupted', activation_fence={run_fence}, updated_at={timestamp},
                metadata_json=json_set(metadata_json, '$.interruption_reason', '{reason}')
            WHERE {scope} AND state IN ('pending','running','paused','waiting')""",
        f"""UPDATE kernel_inbox SET status='discarded'
            WHERE {scope} AND status IN ('accepted','claimed')""",
        f"""UPDATE kernel_interactions SET status='cancelled', outcome='cancelled',
                revision=revision+1, updated_at={timestamp}, event_id=NULL, accepted_seq=NULL
            WHERE {scope} AND status IN ('pending','resolving')""",
    )


async def install_session_delete_trigger(connection: aiosqlite.Connection) -> None:
    # Run in the SessionService writer's transaction, including when deletion
    # is issued by another connection/process with no Kernel object in memory.
    run_fence = """COALESCE((SELECT a.fencing_token FROM kernel_activations a
        WHERE a.agent_instance_id=kernel_runs.agent_instance_id AND a.session_id=OLD.id), activation_fence)"""
    statements = _retire_statements("session_id=OLD.id", "session_deleted", run_fence)
    await connection.execute(f"""
        CREATE TRIGGER IF NOT EXISTS kernel_retire_deleted_session
        AFTER DELETE ON {KSADK_SESSIONS_TABLE}
        BEGIN
            UPDATE kernel_activations SET fencing_token=fencing_token+1
            WHERE session_id=OLD.id AND released=0;
            {'; '.join(statements)};
        END
    """)
    if await _has_waits(connection):
        await install_wait_delete_trigger(connection)


async def retire_missing_session(
    connection: aiosqlite.Connection, schema: str | None, agent_instance_id: str, session_id: str, fence: int,
) -> bool:
    # Called under BEGIN IMMEDIATE after acquiring the scoped activation.
    # An existing session can be legitimately WAITING for an arbitrary time.
    if schema is not None:
        async with connection.execute(
            f"SELECT 1 FROM {schema}.{KSADK_SESSIONS_TABLE} WHERE id=?", (session_id,)
        ) as cursor:
            if await cursor.fetchone() is not None:
                return False
    scope = "agent_instance_id=:agent AND session_id=:session"
    params = {"agent": agent_instance_id, "session": session_id, "fence": fence}
    changed = 0
    for sql in _retire_statements(scope, "session_missing", ":fence"):
        async with connection.execute(sql, params) as cursor:
            changed += cursor.rowcount
    if await _has_waits(connection):
        async with connection.execute(
            "UPDATE mcp_authorization_waits SET status='cancelled' WHERE instance_id=? AND session_id=? "
            "AND status IN ('pending','resolving','unknown','consumed')", (agent_instance_id, session_id),
        ) as cursor:
            changed += cursor.rowcount
    return changed > 0
