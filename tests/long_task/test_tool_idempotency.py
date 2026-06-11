from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.conversations.runtime import invoke_conversation_once
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.tools.gateway import build_tool_receipt_idempotency_key


class _ApprovalRunner:
    def __init__(self):
        self.detection_result = type("Detection", (), {"name": "demo-agent"})()
        self.calls: list[dict] = []

    def prepare_for_request(self, model):
        del model

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "ok"}


def test_tool_receipt_key_is_stable_for_argument_order():
    left = build_tool_receipt_idempotency_key(
        session_id="sess-1",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        tool_call_id="call-1",
        tool_name="write_workspace_file",
        tool_args={"path": "notes.txt", "content": "hello"},
    )
    right = build_tool_receipt_idempotency_key(
        session_id="sess-1",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        tool_call_id="call-1",
        tool_name="write_workspace_file",
        tool_args={"content": "hello", "path": "notes.txt"},
    )

    assert left == right
    assert left.startswith("tool_receipt:")


@pytest.mark.asyncio
async def test_approved_side_effect_tool_replays_receipt_without_second_write(
    monkeypatch,
    tmp_path: Path,
):
    service = InMemorySessionService()
    workspace_ui = tmp_path / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(workspace_ui))
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-tool")
    await service.append_event(
        "sess-tool",
        SessionEvent(
            id="evt-approval",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "approval required"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_write",
                    "tool_name": "write_workspace_file",
                    "arguments": {"path": "notes.txt", "content": "hello"},
                    "run_id": "call_write",
                    "server_label": "ksadk",
                }
            },
            invocation_id="inv-approval",
        ),
    )
    runner = _ApprovalRunner()
    resume_input = {
        "type": "mcp_approval_response",
        "approval_request_id": "appr_write",
        "approve": True,
    }

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-tool",
        messages=[],
        model="demo-model",
        resume_input=resume_input,
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
    )
    target = workspace_ui / "workspace" / "notes.txt"
    assert target.read_text(encoding="utf-8") == "hello"
    target.write_text("changed-by-user", encoding="utf-8")

    await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-tool",
        messages=[],
        model="demo-model",
        resume_input=resume_input,
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
    )

    assert target.read_text(encoding="utf-8") == "changed-by-user"
    events = await service.get_events("sess-tool")
    tool_results = [event for event in events if event.event_type == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[-1].metadata["tool_receipt"]["replayed"] is True
    assert (
        tool_results[-1].metadata["tool_receipt"]["idempotency_key"]
        == tool_results[0].metadata["tool_receipt"]["idempotency_key"]
    )
