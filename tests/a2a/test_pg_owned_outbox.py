"""Real shared-PostgreSQL regression for cross-runtime event consumption."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.task_event_outbox import PostgresA2ATaskEventOutbox
from ksadk.a2a.task_event_outbox_owned import OwnedPostgresA2ATaskEventOutbox


@pytest.mark.asyncio
@pytest.mark.parametrize("different", ["account_id", "tenant_id", "runtime_id"])
async def test_shared_database_outbox_is_owned_and_survives_restart(different):
    dsn = os.getenv("KSADK_A2A_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires local PostgreSQL")
    engine = create_async_engine(dsn)
    identity = {"account_id": uuid4().hex, "tenant_id": "tenant", "runtime_id": "runtime"}
    other = {**identity, different: uuid4().hex}
    a = OwnedPostgresA2ATaskEventOutbox(engine, **identity)
    b = OwnedPostgresA2ATaskEventOutbox(engine, **other)
    legacy = PostgresA2ATaskEventOutbox(engine)
    await a.initialize()
    await b.ensure_writable()
    await legacy.initialize()
    batches = []
    task = "test-owned-" + uuid4().hex
    events = [{"event_id": uuid4().hex, "event_type": "item.completed"}]
    try:
        batch = await a.enqueue(platform_task_id=task, events=events)
        batches.append(batch)
        assert await a.enqueue(platform_task_id=task, events=events) == batch
        assert await b.pending() == []
        assert batch not in {item.batch_id for item in await legacy.pending(limit=10000)}
        # Knowing another runtime's batch ID must not permit any mutation.
        await b.record_failure(batch, "foreign failure")
        await b.dead_letter(batch, "foreign rejection")
        await b.acknowledge(batch)
        restored = OwnedPostgresA2ATaskEventOutbox(engine, **identity)
        pending = await restored.pending()
        assert len(pending) == 1 and pending[0].batch_id == batch
        assert pending[0].events == events and pending[0].attempt_count == 0
        own_b = await b.enqueue(platform_task_id=task, events=events)
        batches.append(own_b)
        assert own_b != batch
        await restored.record_failure(batch, "retryable")
        assert (await a.pending())[0].attempt_count == 1
        await restored.dead_letter(batch, "permanent")
        assert await a.pending() == []
        assert [item.batch_id for item in await b.pending()] == [own_b]
        async with engine.connect() as connection:
            row = (await connection.execute(text(
                "SELECT attempt_count,last_error FROM ksadk_a2a_owned_task_event_dead_letters "
                "WHERE batch_id=:batch"
            ), {"batch": batch})).one()
            assert tuple(row) == (1, "permanent")
        await b.acknowledge(own_b)
        assert await b.pending() == []
    finally:
        async with engine.begin() as connection:
            for batch in batches:
                for table in ("ksadk_a2a_owned_task_event_outbox", "ksadk_a2a_owned_task_event_dead_letters"):
                    await connection.execute(text(f"DELETE FROM {table} WHERE batch_id=:batch"), {"batch": batch})
        await engine.dispose()


def test_owned_outbox_rejects_missing_identity():
    with pytest.raises(ValueError, match="A2A_OUTBOX_OWNER_REQUIRED"):
        OwnedPostgresA2ATaskEventOutbox(None, account_id="account", tenant_id="", runtime_id="runtime")
