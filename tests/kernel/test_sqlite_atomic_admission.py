import sqlite3
from uuid import uuid4

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import AgentControlCommand, ControlSource
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.store import now_iso
from ksadk.sessions._local_tables import KSADK_EVENTS_TABLE
from ksadk.sessions.local_service import LocalSessionService


@pytest.mark.asyncio
async def test_shared_sqlite_admission_and_event_commit_or_rollback_together(tmp_path):
    sessions = LocalSessionService(tmp_path / "shared.sqlite")
    await sessions.create_session("agent", "user", "session")
    events = SessionServiceEventStore(sessions)
    store = SQLiteAgentKernelStore(sessions.db_path, events)
    await store.ensure_schema()
    command = AgentControlCommand(
        command_id=uuid4(),
        idempotency_key="admit",
        tenant_id="tenant",
        agent_instance_id="agent",
        session_id="session",
        command_type="enqueue",
        payload={"content": "work"},
        source=ControlSource(kind="studio", ref="test"),
        authorization_ref="permit",
        submitted_at=now_iso(),
    )
    try:
        with sqlite3.connect(sessions.db_path) as connection:
            connection.execute(
                f"CREATE TRIGGER fail_event BEFORE INSERT ON {KSADK_EVENTS_TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'event write failed'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
            await store.accept_command(command, queue_limit=10)
        assert await store.list_messages("agent") == []
        with sqlite3.connect(sessions.db_path) as connection:
            assert connection.execute("SELECT * FROM kernel_accepted_seq").fetchall() == []
            connection.execute("DROP TRIGGER fail_event")
        receipt = await store.accept_command(command, queue_limit=10)
        assert receipt.status == "accepted" and receipt.accepted_seq == 1
        assert (await store.accept_command(command, queue_limit=10)).status == "duplicate"
        with sqlite3.connect(sessions.db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM kernel_inbox").fetchone()[0] == 1
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {KSADK_EVENTS_TABLE}").fetchone()[0] == 1
            )
    finally:
        await store.close()
