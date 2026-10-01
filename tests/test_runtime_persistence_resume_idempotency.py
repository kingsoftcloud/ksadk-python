from __future__ import annotations

import asyncio

import pytest

from ksadk.conversations.runtime_persistence import (
    append_conversation_event,
    append_run_resume_event,
)
from ksadk.events.canonical import ContinuationResumed, SourceRef
from ksadk.events.canonical_store import runtime_event_to_session_event
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService

pytestmark = pytest.mark.asyncio


async def _append(service, *, invocation_id: str, checkpoint_id: str = "checkpoint-1"):
    return await append_run_resume_event(
        session_id="session-1",
        author="agent-1",
        run_id="run-1",
        checkpoint_id=checkpoint_id,
        resume_attempt_id="attempt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"checkpoint_id": checkpoint_id}},
        invocation_id=invocation_id,
        session_service_provider=lambda: service,
    )


@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_resume_attempt_is_idempotent_across_invocation_retries(tmp_path, backend):
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(tmp_path / "sessions.sqlite")
    )
    await service.create_session("agent-1", "user-1", session_id="session-1")

    first = await _append(service, invocation_id="invocation-a")
    retry = await _append(service, invocation_id="invocation-b")

    assert retry.id == first.id
    assert retry.invocation_id == "invocation-a"
    events = await service.get_events("session-1")
    assert [event.event_type for event in events] == ["run_resume"]


async def test_resume_attempt_collision_fails_loudly(tmp_path):
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await _append(service, invocation_id="invocation-a")

    with pytest.raises(ValueError, match="already bound to a different checkpoint"):
        await _append(service, invocation_id="invocation-b", checkpoint_id="checkpoint-2")


async def test_resume_attempt_reuses_supported_legacy_event_alias():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await append_conversation_event(
        session_id="session-1",
        author="agent-1",
        role="model",
        text="checkpoint resume requested",
        invocation_id="legacy-invocation",
        event_type="runtime_resume",
        content={
            "status": "resuming",
            "run_id": "run-1",
            "checkpoint_id": "checkpoint-1",
            "resume_attempt_id": "attempt-1",
            "framework": "langgraph",
        },
        metadata={
            "run_id": "run-1",
            "checkpoint_id": "checkpoint-1",
            "resume_attempt_id": "attempt-1",
            "framework": "langgraph",
            "framework_ref": {"langgraph": {"checkpoint_id": "checkpoint-1"}},
        },
        session_service_provider=lambda: service,
    )

    result = await _append(service, invocation_id="invocation-a")

    assert result.invocation_id == "legacy-invocation"
    assert len(await service.get_events("session-1")) == 1


async def test_resume_attempt_reuses_canonical_continuation_event():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    canonical = ContinuationResumed(
        schema_version=2,
        event_id="canonical-resume-1",
        seq=0,
        timestamp=1.0,
        run_id="run-1",
        run_seq=1,
        scope_id="run:run-1",
        source=SourceRef(framework="langgraph"),
        continuation_id="checkpoint-1",
        continuation_kind="graph_checkpoint",
        resume_attempt_id="attempt-1",
    )
    await service.append_event(
        "session-1", runtime_event_to_session_event("session-1", canonical)
    )

    result = await _append(service, invocation_id="invocation-a")

    assert result.event_type == "continuation.resumed"
    assert len(await service.get_events("session-1")) == 1


async def test_concurrent_resume_retries_share_one_deterministic_event():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")

    results = await asyncio.gather(
        _append(service, invocation_id="invocation-a"),
        _append(service, invocation_id="invocation-b"),
    )

    assert results[0].id == results[1].id
    events = await service.get_events("session-1")
    assert len(events) == 1
    assert events[0].metadata["resume_attempt_id"] == "attempt-1"


async def test_concurrent_attempt_reuse_for_different_checkpoint_fails_loudly():
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")

    results = await asyncio.gather(
        _append(service, invocation_id="invocation-a", checkpoint_id="checkpoint-1"),
        _append(service, invocation_id="invocation-b", checkpoint_id="checkpoint-2"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    events = await service.get_events("session-1")
    assert len(events) == 1
