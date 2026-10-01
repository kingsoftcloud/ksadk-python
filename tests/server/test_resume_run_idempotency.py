from __future__ import annotations

import pytest

from ksadk.conversations import runtime as conversation_runtime
from ksadk.conversations.runtime_persistence import append_run_resume_event
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.routes import control
from ksadk.server.routes.models import ResumeRunActionRequest
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.mark.asyncio
async def test_resume_retry_resolves_existing_attempt_before_disabled_gate(monkeypatch):
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await append_run_resume_event(
        session_id="session-1",
        author="agent-1",
        run_id="run-1",
        checkpoint_id="checkpoint-1",
        resume_attempt_id="attempt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"checkpoint_id": "checkpoint-1"}},
        invocation_id="invocation-1",
        session_service_provider=lambda: service,
    )

    monkeypatch.setattr(control.deps, "resolve_session_service", lambda: service)
    monkeypatch.setattr(control.deps, "conversation", lambda: conversation_runtime)
    monkeypatch.setattr(
        control,
        "get_runtime_execution",
        lambda: (object(), object()),
    )
    monkeypatch.setattr(
        control,
        "_require_control_session",
        lambda *args, **kwargs: service.get_session_metadata("session-1"),
    )

    async def checkpoint(**_kwargs):
        return {
            "RunId": "run-1",
            "CheckpointId": "checkpoint-1",
            "IsResumable": False,
            "IsTerminal": False,
            "ResumeStatus": "disabled",
            "ResumeDisabledReason": "expired",
            "Framework": "langgraph",
            "FrameworkRef": {"langgraph": {"checkpoint_id": "checkpoint-1"}},
        }

    monkeypatch.setattr(control, "_find_session_checkpoint", checkpoint)
    response = await control.resume_run_action(
        ResumeRunActionRequest(
            AgentId="agent-1",
            UserId="user-1",
            SessionId="session-1",
            RunId="run-1",
            CheckpointId="checkpoint-1",
            ResumeAttemptId="attempt-1",
        ),
        PlatformIdentityContext(),
    )

    assert response["Data"]["Status"] == "already_processed"
    assert response["Data"]["AlreadyProcessed"] is True
