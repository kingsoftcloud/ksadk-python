"""Migration audit counts resume attempts, not their physical carriers."""

import os
from uuid import uuid4

import pytest
import pytest_asyncio

from ksadk.conversations.runtime_persistence import append_conversation_event
from ksadk.events.canonical import ContinuationResumed, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.server.routes.checkpoint_audit import _resume_audit_by_checkpoint
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService


@pytest_asyncio.fixture(params=["memory", "sqlite", "postgres"])
async def audit_service(request, tmp_path):
    if request.param == "memory":
        service = InMemorySessionService()
    elif request.param == "sqlite":
        service = LocalSessionService(tmp_path / "audit.sqlite")
    else:
        dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("KSADK_TEST_POSTGRES_DSN is required for live PostgreSQL")
        import asyncpg

        from ksadk.sessions.postgres_service import PostgresSessionService

        database = f"resume_audit_{uuid4().hex}"
        admin = await asyncpg.connect(dsn)
        await admin.execute(f'CREATE DATABASE "{database}"')
        await admin.close()
        from urllib.parse import urlsplit, urlunsplit

        service = PostgresSessionService(
            dsn=urlunsplit(urlsplit(dsn)._replace(path=f"/{database}"))
        )
    await service.create_session("agent-1", "user-1", session_id="session-1")
    yield service
    if request.param == "postgres":
        await service.aclose()
        admin = await asyncpg.connect(dsn)
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


async def append_legacy(service, event_type, attempt):
    return await append_conversation_event(
        session_id="session-1",
        author="agent-1",
        role="model",
        text="",
        event_type=event_type,
        invocation_id="run-1",
        metadata={"run_id": "run-1", "checkpoint_id": "checkpoint-1", "resume_attempt_id": attempt},
        session_service_provider=lambda: service,
    )


async def assert_audit(service, expected):
    events = await service.get_events("session-1")
    rest = _resume_audit_by_checkpoint(events)[("run-1", "checkpoint-1")]
    batch = await service.get_checkpoint_stats([("session-1", "run-1", "checkpoint-1")])
    lookup = await service.get_checkpoint_lookup_stats("session-1", "run-1", "checkpoint-1")
    assert rest["resume_count"] == expected
    assert batch["audits"][("session-1", "run-1", "checkpoint-1")]["resume_count"] == expected
    assert lookup["resume_count"] == expected


@pytest.mark.asyncio
async def test_runtime_resume_alias_is_audited(audit_service):
    await append_legacy(audit_service, "runtime_resume", "attempt-1")
    await assert_audit(audit_service, 1)


@pytest.mark.asyncio
async def test_mixed_carriers_share_one_resume_attempt(audit_service):
    for event_type in ("run_resume", "runtime_resume"):
        await append_legacy(audit_service, event_type, "attempt-1")
    store = RuntimeEventStore(audit_service)
    for event_id in ("canonical-a", "canonical-b"):
        await store.append(
            "session-1",
            [
                ContinuationResumed(
                    schema_version=2,
                    event_id=event_id,
                    seq=0,
                    timestamp=101.0,
                    run_id="run-1",
                    scope_id="run:run-1",
                    source=SourceRef(framework="langgraph"),
                    continuation_id="checkpoint-1",
                    continuation_kind="graph_checkpoint",
                    resume_attempt_id="attempt-1",
                )
            ],
        )
    await assert_audit(audit_service, 1)


@pytest.mark.asyncio
async def test_distinct_and_unidentified_legacy_attempts_stay_separate(audit_service):
    for event_type, attempt in (
        ("run_resume", "attempt-1"),
        ("runtime_resume", "attempt-2"),
        ("run_resume", None),
        ("runtime_resume", None),
    ):
        await append_legacy(audit_service, event_type, attempt)
    await assert_audit(audit_service, 4)
