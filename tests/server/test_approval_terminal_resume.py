"""Public approval retries cannot resurrect a rejected or absent request."""

from types import SimpleNamespace

import pytest

from ksadk.conversations import runtime_resume
from ksadk.conversations.runtime_persistence import append_conversation_event
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.routes import openai_compat
from ksadk.server.routes.models import ResponsesRequest
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["rejected", "cancelled", "absent"])
@pytest.mark.parametrize("other_pending", [False, True])
async def test_late_public_approve_never_creates_a_claim(monkeypatch, terminal, other_pending):
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")

    async def append(kind, metadata):
        await append_conversation_event(
            session_id="session-1", author="user", role="user", text="",
            invocation_id="run-1", event_type=kind, metadata=metadata,
            session_service_provider=lambda: service,
        )

    if terminal != "absent":
        await append("approval_request", {"interrupt_info": {
            "approval_request_id": "approval-1", "tool_name": "write_workspace_file",
            "arguments": {"path": "result.txt", "content": "done"}, "run_id": "run-1",
        }})
        await append("approval_response", {"resume_input": {
            "type": "mcp_approval_response", "approval_request_id": "approval-1",
            "approve": False, "status": terminal,
        }})
    if other_pending:
        await append("approval_request", {"interrupt_info": {
            "approval_request_id": "other-approval", "tool_name": "write_workspace_file",
            "arguments": {"path": "other.txt", "content": "other"}, "run_id": "other-run",
        }})
    before = await service.get_events("session-1")
    calls = []
    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable",
                        lambda _: lambda **kwargs: calls.append(kwargs) or {"ok": True})
    claim = service.claim_tool_receipt

    async def record_claim(*args, **kwargs):
        calls.append("claim")
        return await claim(*args, **kwargs)

    monkeypatch.setattr(service, "claim_tool_receipt", record_claim)
    monkeypatch.setattr(openai_compat.deps, "resolve_session_service", lambda: service)
    monkeypatch.setattr(openai_compat, "get_runtime_execution", lambda: (
        object(), SimpleNamespace(runtime_type="", detection=SimpleNamespace(name="agent-1")),
    ))

    async def invoke(**kwargs):
        prepared = await build_run_input(
            agent_id=kwargs["agent_id"], user_id=kwargs["user_id"],
            session_id=kwargs["session_id"], messages=kwargs["messages"],
            resume_input=kwargs["resume_input"], invocation_id=kwargs.get("invocation_id"),
            session_service_provider=kwargs["session_service_provider"],
        )
        return prepared.session_id, {"output_text": "ok"}

    monkeypatch.setattr(openai_compat, "invoke_runtime_conversation_once", invoke)
    request = ResponsesRequest(
        input=[{"type": "mcp_approval_response", "approval_request_id": "approval-1",
                "checkpoint_id": "checkpoint-1", "approve": True}],
        session_id="session-1", user="user-1",
    )
    with pytest.raises(ValueError, match="pending approval_request"):
        await openai_compat.responses(request, PlatformIdentityContext())
    assert calls == []
    assert await service.get_events("session-1") == before
