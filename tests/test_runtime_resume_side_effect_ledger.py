from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.conversations import runtime_resume
from ksadk.conversations.runtime_persistence import append_conversation_event
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.routes import openai_compat
from ksadk.server.routes.models import ResponsesRequest
from ksadk.sessions.in_memory import InMemorySessionService


def _resume_input(checkpoint_id: str) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "checkpoint_id": checkpoint_id,
        "tool_name": "write_workspace_file",
        "tool_args": {"path": "result.txt", "content": "done"},
        "approval": {"approved": True, "approval_request_id": f"approval-{checkpoint_id}"},
    }


@pytest.mark.asyncio
async def test_approved_builtin_resume_replays_receipt_without_reexecuting(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    calls: list[dict[str, object]] = []

    def write_workspace_file(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "path": kwargs["path"]}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: write_workspace_file)
    resume_input = _resume_input("checkpoint-1")

    first = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-1",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )
    second = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-2",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    assert calls == [{"path": "result.txt", "content": "done"}]
    assert first["output"] == {"ok": True, "path": "result.txt"}
    assert second["output"] == {"ok": True, "path": "result.txt", "replayed": True}
    events = await service.get_events("session-1")
    assert len(events) == 2
    assert events[-1].metadata["replayed"] is True


@pytest.mark.asyncio
async def test_approved_builtin_resume_isolated_by_checkpoint(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    calls: list[str] = []

    def write_workspace_file(**kwargs):
        calls.append(kwargs["content"])
        return {"ok": True, "content": kwargs["content"]}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: write_workspace_file)
    first = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-1",
        resume_input=_resume_input("checkpoint-1"),
        session_service_provider=lambda: service,
    )
    second = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-2",
        resume_input={
            **_resume_input("checkpoint-2"),
            "tool_args": {"path": "result.txt", "content": "new"},
        },
        session_service_provider=lambda: service,
    )

    assert calls == ["done", "new"]
    assert first["output"] == {"ok": True, "content": "done"}
    assert second["output"] == {"ok": True, "content": "new"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_input",
    [
        {
            **_resume_input("checkpoint-reject"),
            "approval": {"approved": False, "reason": "user rejected"},
        },
        {
            **_resume_input("checkpoint-cancel"),
            "type": "cancel",
            "approval": None,
        },
    ],
)
async def test_rejected_or_cancelled_resume_has_no_builtin_side_effect(
    monkeypatch, resume_input
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    calls: list[dict[str, object]] = []

    def write_workspace_file(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: write_workspace_file)
    result = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-rejected",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    assert result is None
    assert calls == []
    assert await service.get_events("session-1") == []


@pytest.mark.asyncio
async def test_corrupt_persisted_receipt_fails_loud_without_replaying_output(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-corrupt")
    tool_args = dict(resume_input["tool_args"])
    receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-1",
        tool_name="write_workspace_file",
        tool_args=tool_args,
        tool_call_id="run-1",
        checkpoint_id="checkpoint-corrupt",
    )
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text="tampered",
        invocation_id="invocation-old",
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata={
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "other.txt", "content": "tampered"},
            "tool_output": {"ok": True, "content": "tampered"},
            "tool_receipt": receipt,
        },
    )
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("corrupt receipt must not execute builtin"),
    )

    with pytest.raises(ValueError, match="tool_args"):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-new",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )


@pytest.mark.asyncio
async def test_legacy_unscoped_receipt_replays_without_checkpoint(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        key: value for key, value in _resume_input("").items() if key != "checkpoint_id"
    }
    tool_args = dict(resume_input["tool_args"])
    legacy_receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-1",
        tool_name="write_workspace_file",
        tool_args=tool_args,
        tool_call_id="run-1",
    )
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text="legacy",
        invocation_id="invocation-old",
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata={
            "tool_name": "write_workspace_file",
            "tool_args": tool_args,
            "tool_output": {"ok": True, "legacy": True},
            "tool_receipt": legacy_receipt,
        },
    )
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("legacy receipt should replay without executing builtin"),
    )

    result = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-new",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    assert result["output"] == {"ok": True, "legacy": True, "replayed": True}
    events = await service.get_events("session-1")
    assert events[-1].metadata["tool_receipt"]["checkpoint_id"] == ""


@pytest.mark.asyncio
async def test_explicit_checkpoint_rejects_ambiguous_legacy_receipt_without_side_effect(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-ambiguous")
    tool_args = dict(resume_input["tool_args"])
    legacy_receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-1",
        tool_name="write_workspace_file",
        tool_args=tool_args,
        tool_call_id="run-1",
    )
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text="legacy",
        invocation_id="invocation-old",
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata={
            "tool_name": "write_workspace_file",
            "tool_args": tool_args,
            "tool_output": {"ok": True, "legacy": True},
            "tool_receipt": legacy_receipt,
        },
    )
    calls: list[dict[str, object]] = []

    def write_workspace_file(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: write_workspace_file)

    with pytest.raises(
        runtime_resume.LegacyToolReceiptCheckpointAmbiguityError,
        match=runtime_resume.LEGACY_TOOL_RECEIPT_CHECKPOINT_AMBIGUITY_MESSAGE,
    ):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-new",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )

    assert calls == []
    events = await service.get_events("session-1")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_build_run_input_rejects_ambiguous_legacy_receipt_before_approval_event(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        **_resume_input("checkpoint-route-ambiguous"),
        "type": "ksadk_resume",
    }
    tool_args = dict(resume_input["tool_args"])
    legacy_tool_args = {**tool_args, "approval": dict(resume_input["approval"])}
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="model",
        text="approval requested",
        invocation_id="invocation-old",
        event_type="approval_request",
        session_service_provider=lambda: service,
        metadata={
            "interrupt_info": {
                "approval_request_id": resume_input["approval"]["approval_request_id"],
                "id": resume_input["approval"]["approval_request_id"],
                "tool_name": "write_workspace_file",
                "arguments": legacy_tool_args,
                "run_id": "run-1",
            }
        },
    )
    legacy_receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-1",
        tool_name="write_workspace_file",
        tool_args=legacy_tool_args,
        tool_call_id="run-1",
    )
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text="legacy",
        invocation_id="invocation-old",
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata={
            "tool_name": "write_workspace_file",
            "tool_args": legacy_tool_args,
            "tool_output": {"ok": True, "legacy": True},
            "tool_receipt": legacy_receipt,
        },
    )
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("ambiguous legacy receipt must not execute builtin"),
    )

    with pytest.raises(
        runtime_resume.LegacyToolReceiptCheckpointAmbiguityError,
        match=runtime_resume.LEGACY_TOOL_RECEIPT_CHECKPOINT_AMBIGUITY_MESSAGE,
    ):
        await build_run_input(
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
            messages=[],
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )

    events = await service.get_events("session-1")
    assert len(events) == 2
    assert [event.event_type for event in events] == ["approval_request", "tool_result"]


@pytest.mark.asyncio
async def test_openai_responses_route_rejects_ambiguous_legacy_checkpoint_resume(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    approval = {"approved": True, "approval_request_id": "approval-route"}
    tool_args = {"path": "result.txt", "content": "done"}
    legacy_tool_args = {**tool_args, "approval": approval}
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="model",
        text="approval requested",
        invocation_id="invocation-old",
        event_type="approval_request",
        session_service_provider=lambda: service,
        metadata={
            "interrupt_info": {
                "approval_request_id": "approval-route",
                "id": "approval-route",
                "tool_name": "write_workspace_file",
                "arguments": tool_args,
                "run_id": "run-route",
            }
        },
    )
    legacy_receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-route",
        tool_name="write_workspace_file",
        tool_args=legacy_tool_args,
        tool_call_id="run-route",
    )
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text="legacy",
        invocation_id="invocation-old",
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata={
            "tool_name": "write_workspace_file",
            "tool_args": legacy_tool_args,
            "tool_output": {"ok": True, "legacy": True},
            "tool_receipt": legacy_receipt,
        },
    )
    monkeypatch.setattr(
        openai_compat,
        "get_runtime_execution",
        lambda: (
            object(),
            SimpleNamespace(runtime_type="", detection=SimpleNamespace(name="agent-1")),
        ),
    )
    monkeypatch.setattr(openai_compat.deps, "resolve_session_service", lambda: service)
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("ambiguous legacy receipt must not execute builtin"),
    )

    request = ResponsesRequest(
        input=[
            {
                "type": "mcp_approval_response",
                "approval_request_id": "approval-route",
                "approve": True,
                "checkpoint_id": "checkpoint-route",
            }
        ],
        session_id="session-1",
        user="user-1",
    )
    with pytest.raises(
        runtime_resume.LegacyToolReceiptCheckpointAmbiguityError,
        match=runtime_resume.LEGACY_TOOL_RECEIPT_CHECKPOINT_AMBIGUITY_MESSAGE,
    ):
        await openai_compat.responses(request, PlatformIdentityContext())

    events = await service.get_events("session-1")
    assert len(events) == 2
    assert [event.event_type for event in events] == ["approval_request", "tool_result"]
