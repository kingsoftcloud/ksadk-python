from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.context_store import A2AContextIdentity, PostgresA2AContextStore
from ksadk.a2a.resume_store import A2AResumeState, PostgresA2AResumeStateStore
from ksadk.a2a.task_event_outbox import PostgresA2ATaskEventOutbox
from ksadk.runtime import ResumeTarget, RunHandle


def _dsn() -> str:
    dsn = os.environ.get("KSADK_A2A_TEST_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("KSADK_A2A_TEST_DATABASE_URL must point to PostgreSQL")
    assert dsn.startswith("postgresql+asyncpg://")
    return dsn


@pytest.mark.asyncio
async def test_managed_postgres_stores_survive_new_instances() -> None:
    engine = create_async_engine(_dsn())
    test_id = uuid4().hex
    identity = A2AContextIdentity(
        account_id=f"r85-account-{test_id}",
        tenant_id=f"r85-tenant-{test_id}",
        caller_principal_type="runtime",
        caller_principal_id=f"r85-caller-{test_id}",
    )
    state = A2AResumeState(
        handle=RunHandle(
            run_id=f"r85-run-{test_id}",
            session_id=f"r85-session-{test_id}",
            runtime_type="test",
            native_ref={"checkpoint_id": f"r85-checkpoint-{test_id}"},
        ),
        target=ResumeTarget(kind="checkpoint_id", id=f"r85-checkpoint-{test_id}"),
        payload_kind="free_text",
    )
    resume_key = f"r85-resume-key-{test_id}"
    platform_task_id = f"a2a-task-r85-{test_id}"
    dead_platform_task_id = f"a2a-task-r85-dead-{test_id}"
    try:
        context = PostgresA2AContextStore(engine)
        resume = PostgresA2AResumeStateStore(engine)
        outbox = PostgresA2ATaskEventOutbox(engine)
        await context.initialize()
        await resume.initialize()
        await outbox.initialize()

        session_id = await context.resolve_or_create(identity, f"r85-context-{test_id}")
        await resume.put(resume_key, state)
        batch_id = await outbox.enqueue(
            platform_task_id=platform_task_id,
            events=[{"type": "task.started", "seq": 1}],
        )
        dead_batch_id = await outbox.enqueue(
            platform_task_id=dead_platform_task_id,
            events=[{"type": "task.failed", "seq": 2}],
        )

        assert (
            await PostgresA2AContextStore(engine).get(identity, f"r85-context-{test_id}")
            == session_id
        )
        assert await PostgresA2AResumeStateStore(engine).get(resume_key) == state
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ksadk_a2a_task_event_outbox "
                        "WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": batch_id},
                )
            ).scalar_one() == 1

        await outbox.record_failure(dead_batch_id, "A2A_EVENT_REF_INVALID")
        await outbox.dead_letter(dead_batch_id, "A2A_EVENT_REF_INVALID")
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM ksadk_a2a_task_event_outbox "
                        "WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": dead_batch_id},
                )
            ).scalar_one() == 0
            dead_letter = (
                await connection.execute(
                    text(
                        "SELECT platform_task_id, attempt_count, last_error, events_json "
                        "FROM ksadk_a2a_task_event_dead_letters "
                        "WHERE batch_id = :batch_id"
                    ),
                    {"batch_id": dead_batch_id},
                )
            ).one()
        assert dead_letter.platform_task_id == dead_platform_task_id
        assert dead_letter.attempt_count == 1
        assert dead_letter.last_error == "A2A_EVENT_REF_INVALID"
        assert '"task.failed"' in dead_letter.events_json

        await PostgresA2AResumeStateStore(engine).delete(resume_key)
        await PostgresA2ATaskEventOutbox(engine).acknowledge(batch_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM ksadk_a2a_context_mappings "
                    "WHERE internal_session_id = :session_id"
                ),
                {"session_id": locals().get("session_id", "")},
            )
            await connection.execute(
                text(
                    "DELETE FROM ksadk_a2a_task_event_outbox "
                    "WHERE platform_task_id IN (:platform_task_id, :dead_platform_task_id)"
                ),
                {
                    "platform_task_id": platform_task_id,
                    "dead_platform_task_id": dead_platform_task_id,
                },
            )
            await connection.execute(
                text(
                    "DELETE FROM ksadk_a2a_task_event_dead_letters "
                    "WHERE platform_task_id = :dead_platform_task_id"
                ),
                {"dead_platform_task_id": dead_platform_task_id},
            )
        await engine.dispose()
