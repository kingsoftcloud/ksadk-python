from __future__ import annotations

from dataclasses import asdict

import pytest

from ksadk.runtime.adapter import CONVERSATION_PREPROCESSING_METADATA_KEY, StartRequest
from ksadk.runtime.preprocessing import PreparedConversationTurn, prepare_runtime_start


class _Runner:
    detection_result = type(
        "Detection",
        (),
        {"name": "fixture", "type": type("Type", (), {"value": "langgraph"})()},
    )()


def _prepared_turn() -> PreparedConversationTurn:
    return PreparedConversationTurn(
        session_id="session-1",
        invocation_id="run-1",
        user_input="current",
        user_display_input="current",
        history=[{"role": "user", "content": "previous"}],
        input_content=[{"type": "input_text", "text": "current"}],
        input_messages=[{"role": "user", "content": "current"}],
        user_parts=[{"text": "current"}],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        request_history=[{"role": "user", "content": "previous"}],
        request_responses_history=[
            {"role": "user", "content": "previous"},
            {"role": "user", "content": "current"},
        ],
        responses_history=[
            {"role": "user", "content": "previous"},
            {"role": "user", "content": "current"},
        ],
    )


@pytest.mark.asyncio
async def test_prepared_conversation_turn_is_not_built_twice(monkeypatch) -> None:
    async def fail_if_rebuilt(**_kwargs):
        raise AssertionError("prepared conversation turn must not be rebuilt")

    monkeypatch.setattr("ksadk.runtime.preprocessing.build_run_input", fail_if_rebuilt)
    request = StartRequest(
        input="current",
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        metadata={
            "invocation_id": "run-1",
            CONVERSATION_PREPROCESSING_METADATA_KEY: {
                "messages": [{"role": "user", "content": "current"}],
                "prepared_turn": asdict(_prepared_turn()),
            },
        },
    )

    prepared = await prepare_runtime_start(request, _Runner())

    assert prepared is not None
    assert prepared.input_text == "current"
    assert prepared.runner_input["history"] == [{"role": "user", "content": "previous"}]
    assert prepared.runner_input["input"] == "current"


@pytest.mark.asyncio
async def test_request_scoped_tool_approval_mode_reaches_runtime_context() -> None:
    request = StartRequest(
        input="current",
        user_id="user-1",
        session_id="session-1",
        agent_id="agent-1",
        metadata={
            "invocation_id": "run-1",
            CONVERSATION_PREPROCESSING_METADATA_KEY: {
                "messages": [{"role": "user", "content": "current"}],
                "request_metadata": {"tool_approval_mode": "ask"},
                "prepared_turn": asdict(_prepared_turn()),
            },
        },
    )

    prepared = await prepare_runtime_start(request, _Runner())

    assert prepared is not None
    assert prepared.context.tool_approval_mode == "ask"
