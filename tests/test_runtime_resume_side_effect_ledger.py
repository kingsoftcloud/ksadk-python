from __future__ import annotations

import asyncio
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


async def _append_receipt(
    service: InMemorySessionService,
    resume_input: dict[str, object],
    *,
    output: object = None,
    status: str = "completed",
    include_output: bool = True,
    invocation_id: str = "invocation-old",
) -> None:
    receipt = runtime_resume._tool_receipt_metadata(
        session_id="session-1",
        run_id="run-1",
        tool_name="write_workspace_file",
        tool_args=dict(resume_input["tool_args"]),
        tool_call_id="run-1",
        checkpoint_id=str(resume_input["checkpoint_id"]),
        status=status,
    )
    metadata: dict[str, object] = {
        "tool_name": "write_workspace_file",
        "tool_args": dict(resume_input["tool_args"]),
        "tool_receipt": receipt,
    }
    if include_output:
        metadata["tool_output"] = output
    await append_conversation_event(
        session_id="session-1",
        author="tool",
        role="user",
        text=str(output),
        invocation_id=invocation_id,
        event_type="tool_result",
        session_service_provider=lambda: service,
        metadata=metadata,
    )

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
async def test_concurrent_builtin_resumes_claim_once(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    calls: list[dict[str, object]] = []
    def run_command(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: run_command)
    resume_input = {**_resume_input("checkpoint-concurrent"), "tool_name": "run_command"}
    results = await asyncio.gather(
        runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-1",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        ),
        runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-2",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        ),
        return_exceptions=True,
    )

    assert len(calls) == 1
    assert (
        sum(isinstance(result, runtime_resume.ToolReceiptUncertainError) for result in results)
        == 1
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    receipts = [
        event
        for event in await service.get_events("session-1")
        if event.event_type == "tool_result"
    ]
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_crash_after_builtin_leaves_unknown_without_reexecution(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    calls: list[dict[str, object]] = []

    def write_workspace_file(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runtime_resume, "_builtin_tool_callable", lambda _: write_workspace_file)
    original_settle = service.settle_tool_receipt
    failed_once = True

    async def fail_settle(*args, **kwargs):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise RuntimeError("injected crash before receipt settlement")
        return await original_settle(*args, **kwargs)

    monkeypatch.setattr(service, "settle_tool_receipt", fail_settle)
    with pytest.raises(RuntimeError, match="injected crash"):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-1",
            resume_input=_resume_input("checkpoint-crash"),
            session_service_provider=lambda: service,
        )

    with pytest.raises(runtime_resume.ToolReceiptUncertainError):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-2",
            resume_input=_resume_input("checkpoint-crash"),
            session_service_provider=lambda: service,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_responses_retry_unknown_claim_does_not_append_duplicate_approval(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await append_conversation_event(
        session_id="session-1",
        author="model",
        role="model",
        text="approval requested",
        invocation_id="invocation-request",
        event_type="approval_request",
        session_service_provider=lambda: service,
        metadata={
            "interrupt_info": {
                "approval_request_id": "approval-route-retry",
                "tool_name": "write_workspace_file",
                "arguments": '{"path":"result.txt","content":"done"}',
                "run_id": "run-1",
            }
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
        runtime_resume, "_builtin_tool_callable", lambda _: lambda **_: {"ok": True}
    )

    async def fake_invoke(**kwargs):
        prepared = await build_run_input(
            agent_id=kwargs["agent_id"],
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            messages=kwargs["messages"],
            model=kwargs.get("model"),
            model_metadata=kwargs.get("model_metadata"),
            model_options=kwargs.get("model_options"),
            instructions=kwargs.get("instructions"),
            request_metadata=kwargs.get("request_metadata"),
            custom_metadata=kwargs.get("custom_metadata"),
            resume_input=kwargs["resume_input"],
            invocation_id=kwargs.get("invocation_id"),
            session_service_provider=kwargs["session_service_provider"],
        )
        return prepared.session_id, {"output_text": "ok"}

    monkeypatch.setattr(openai_compat, "invoke_runtime_conversation_once", fake_invoke)
    original_settle = service.settle_tool_receipt
    failed_once = True

    async def fail_settle(*args, **kwargs):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise RuntimeError("injected settle crash")
        return await original_settle(*args, **kwargs)

    monkeypatch.setattr(service, "settle_tool_receipt", fail_settle)
    request = ResponsesRequest(
        input=[
            {
                "type": "mcp_approval_response",
                "approval_request_id": "approval-route-retry",
                "approve": True,
            }
        ],
        session_id="session-1",
        user="user-1",
    )
    with pytest.raises(RuntimeError, match="injected settle crash"):
        await openai_compat.responses(request, PlatformIdentityContext())
    before_retry = await service.get_events("session-1")
    with pytest.raises(runtime_resume.ToolReceiptUncertainError) as retry_error:
        await openai_compat.responses(request, PlatformIdentityContext())
    assert "idempotency_key=tool_receipt:" in str(retry_error.value)
    assert await service.get_events("session-1") == before_retry


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
async def test_accepted_not_extracted_receipt_replays_as_completed(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: lambda **_: {"ok": False, "status": "accepted_not_extracted"},
    )
    resume_input = _resume_input("checkpoint-accepted-not-extracted")
    await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-1",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )
    replay = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-2",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )
    assert replay["output"] == {"ok": False, "status": "accepted_not_extracted", "replayed": True}


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "output"),
    [
        ("completed", {"ok": False, "error": "failed"}),
        ("failed", {"ok": True, "value": "done"}),
        ("unknown", {"ok": True, "value": "done"}),
    ],
)
async def test_corrupt_receipt_output_integrity_fails_before_builtin(
    monkeypatch, status: str, output: object
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-invalid")
    await _append_receipt(service, resume_input, output=output, status=status)
    before = await service.get_events("session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("invalid receipt must not execute builtin"),
    )

    with pytest.raises(ValueError, match="receipt"):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-new",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )

    assert await service.get_events("session-1") == before


@pytest.mark.asyncio
async def test_missing_receipt_output_fails_before_replay_append(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-missing-output")
    await _append_receipt(service, resume_input, include_output=False)
    before = await service.get_events("session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("missing output must not execute builtin"),
    )

    with pytest.raises(ValueError, match="missing tool_output"):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-new",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )

    assert await service.get_events("session-1") == before


@pytest.mark.asyncio
async def test_failed_receipt_replays_failure_without_reexecuting(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-failed")
    output = {"ok": False, "error_type": "RuntimeError", "error_message": "denied"}
    await _append_receipt(service, resume_input, output=output, status="failed")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("failed receipt must replay without executing builtin"),
    )

    result = await runtime_resume._execute_approved_builtin_tool_resume(
        session_id="session-1",
        invocation_id="invocation-new",
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    assert result["output"] == {**output, "replayed": True}
    events = await service.get_events("session-1")
    assert len(events) == 2
    assert events[-1].metadata["tool_receipt"]["status"] == "failed"


@pytest.mark.asyncio
async def test_non_mapping_and_legacy_success_statuses_replay(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("receipt replay must not execute builtin"),
    )

    for index, (status, output) in enumerate(
        (("completed", "ok"), ("succeeded", None)),
        start=1,
    ):
        resume_input = _resume_input(f"checkpoint-scalar-{index}")
        await _append_receipt(service, resume_input, output=output, status=status)
        result = await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id=f"invocation-new-{index}",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )
        assert result["output"] == output


@pytest.mark.asyncio
async def test_newest_malformed_receipt_wins_over_older_valid_receipt(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = _resume_input("checkpoint-newest")
    await _append_receipt(
        service,
        resume_input,
        output={"ok": True, "value": "old"},
        status="completed",
        invocation_id="invocation-old",
    )
    await _append_receipt(
        service,
        resume_input,
        output={"ok": False, "error": "tampered"},
        status="completed",
        invocation_id="invocation-newest",
    )
    before = await service.get_events("session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("malformed newest receipt must not execute builtin"),
    )

    with pytest.raises(ValueError, match="status"):
        await runtime_resume._execute_approved_builtin_tool_resume(
            session_id="session-1",
            invocation_id="invocation-replay",
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )
    assert await service.get_events("session-1") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "output", "include_output", "error_match"),
    [
        ("completed", {"ok": False}, True, "status"),
        ("failed", {"ok": True}, True, "status"),
        ("unknown", {"ok": True}, True, "unknown status"),
        ("completed", None, False, "missing tool_output"),
    ],
)
async def test_build_run_input_rejects_corrupt_receipt_before_approval_append(
    monkeypatch,
    status: str,
    output: object,
    include_output: bool,
    error_match: str,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        **_resume_input("checkpoint-public-invalid"),
        "type": "mcp_approval_response",
    }
    await _append_receipt(
        service,
        resume_input,
        output=output,
        status=status,
        include_output=include_output,
    )
    before = await service.get_events("session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("invalid receipt must not execute builtin"),
    )

    with pytest.raises(ValueError, match=error_match):
        await build_run_input(
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
            messages=[],
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )

    # The route preflight runs before the approval response append and the
    # direct executor, so the malformed ledger entry remains the only event.
    assert await service.get_events("session-1") == before


@pytest.mark.asyncio
async def test_build_run_input_rejects_corrupt_receipt_before_approval_append_for_pending_resume(
    monkeypatch,
) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        **_resume_input("checkpoint-public-pending-invalid"),
        "type": "mcp_approval_response",
    }
    await append_conversation_event(
        session_id="session-1",
        author="model",
        role="model",
        text="approval requested",
        invocation_id="invocation-request",
        event_type="approval_request",
        session_service_provider=lambda: service,
        metadata={
            "interrupt_info": {
                "approval_request_id": "approval-checkpoint-public-pending-invalid",
                "tool_name": "write_workspace_file",
                "arguments": '{"path":"result.txt","content":"done"}',
                "run_id": "run-1",
            }
        },
    )
    await _append_receipt(
        service,
        {
            **resume_input,
            "tool_args": {
                **dict(resume_input["tool_args"]),
                "approval": resume_input["approval"],
            },
        },
        output={"ok": False},
        status="completed",
    )
    before = await service.get_events("session-1")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("invalid receipt must not execute builtin"),
    )

    with pytest.raises(ValueError, match="status"):
        await build_run_input(
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
            messages=[],
            resume_input=resume_input,
            session_service_provider=lambda: service,
        )
    assert await service.get_events("session-1") == before


@pytest.mark.asyncio
async def test_build_run_input_rejection_skips_corrupt_receipt_preflight(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        **_resume_input("checkpoint-public-reject"),
        "type": "mcp_approval_response",
        "approval": {"approved": False, "reason": "no thanks"},
    }
    await _append_receipt(
        service,
        resume_input,
        output={"ok": False},
        status="completed",
    )
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("rejected receipt must not execute builtin"),
    )

    result = await build_run_input(
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        messages=[],
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    events = await service.get_events("session-1")
    assert [event.event_type for event in events] == ["tool_result", "approval_response"]
    assert result.resume_input == resume_input


@pytest.mark.asyncio
async def test_build_run_input_replays_valid_failed_receipt_without_builtin(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    resume_input = {
        **_resume_input("checkpoint-public-failed"),
        "type": "mcp_approval_response",
    }
    output = {"ok": False, "error_type": "PermissionError", "error_message": "denied"}
    await _append_receipt(service, resume_input, output=output, status="failed")
    monkeypatch.setattr(
        runtime_resume,
        "_builtin_tool_callable",
        lambda _: pytest.fail("valid failed receipt must not execute builtin"),
    )

    prepared = await build_run_input(
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        messages=[],
        resume_input=resume_input,
        session_service_provider=lambda: service,
    )

    assert prepared.resume_input["output"] == {**output, "replayed": True}
    events = await service.get_events("session-1")
    assert [event.event_type for event in events] == [
        "tool_result",
        "approval_response",
        "tool_result",
    ]
    assert events[-1].metadata["tool_output"] == {**output, "replayed": True}
