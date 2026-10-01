from __future__ import annotations

import pytest

from ksadk.conversations import runtime_resume
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
