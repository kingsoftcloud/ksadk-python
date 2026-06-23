from __future__ import annotations

from ksadk.conversations.context import build_history_from_events
from ksadk.conversations.runtime import (
    append_run_checkpoint_event,
    append_run_resume_event,
    extract_responses_resume_input,
    invoke_conversation_once,
)
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService

import pytest


class _CheckpointResumeRunner:
    def __init__(self):
        self.detection_result = type("Detection", (), {"name": "demo-agent"})()
        self.calls: list[dict] = []

    def prepare_for_request(self, model):
        del model

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {
            "output": "resumed",
            "metadata": {
                "agentengine": {
                    "run_id": input_data["run_id"],
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": "tenant:agent:sess-long",
                            "checkpoint_id": "ckpt-after",
                        }
                    },
                }
            },
        }


def test_checkpoint_runtime_events_are_not_projected_to_model_history():
    events = [
        SessionEvent(
            id="evt-checkpoint",
            author="demo-agent",
            event_type="run_checkpoint",
            content={"status": "saved"},
            metadata={"run_id": "run-1", "checkpoint_id": "ckpt-1"},
            seq_id=1,
        ),
        SessionEvent(
            id="evt-resume",
            author="demo-agent",
            event_type="run_resume",
            content={"status": "requested"},
            metadata={"run_id": "run-1", "resume_attempt_id": "resume-1"},
            seq_id=2,
        ),
        SessionEvent(
            id="evt-user",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "继续"}]},
            seq_id=3,
        ),
    ]

    assert build_history_from_events(events) == [{"role": "user", "content": "继续"}]


def test_responses_input_accepts_checkpoint_resume_action():
    resume_input = extract_responses_resume_input(
        [
            {
                "type": "agentengine.resume_checkpoint",
                "run_id": "run-1",
                "checkpoint_id": "ckpt-before",
                "resume_attempt_id": "resume-1",
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "thread_id": "tenant:agent:sess-long",
                        "checkpoint_id": "ckpt-before",
                    }
                },
            }
        ]
    )

    assert resume_input["type"] == "agentengine.resume_checkpoint"
    assert resume_input["run_id"] == "run-1"
    assert resume_input["checkpoint_id"] == "ckpt-before"
    assert resume_input["resume_attempt_id"] == "resume-1"


@pytest.mark.asyncio
async def test_checkpoint_resume_keeps_same_run_id_and_records_attempt(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-long")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    checkpoint = await append_run_checkpoint_event(
        session_id="sess-long",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-before",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-long",
                "checkpoint_id": "ckpt-before",
            }
        },
        phase="tool_result",
        invocation_id="inv-checkpoint",
    )
    resume = await append_run_resume_event(
        session_id="sess-long",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-before",
        resume_attempt_id="resume-1",
        framework="langgraph",
        framework_ref=checkpoint.metadata["framework_ref"],
        invocation_id="inv-resume",
    )

    assert checkpoint.metadata["run_id"] == resume.metadata["run_id"] == "run-1"
    assert resume.metadata["resume_attempt_id"] == "resume-1"

    runner = _CheckpointResumeRunner()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-long",
        messages=[],
        model="demo-model",
        prepare_runner=lambda active_runner, model: active_runner.prepare_for_request(model),
        resume_input={
            "type": "agentengine.resume_checkpoint",
            "run_id": "run-1",
            "checkpoint_id": "ckpt-before",
            "resume_attempt_id": "resume-1",
            "framework": "langgraph",
            "framework_ref": checkpoint.metadata["framework_ref"],
        },
    )

    assert runner.calls[0]["checkpoint_resume"] is True
    assert runner.calls[0]["run_id"] == "run-1"
    assert result["metadata"]["agentengine"]["run_id"] == "run-1"
    assert (
        result["metadata"]["agentengine"]["framework_ref"]["langgraph"]["checkpoint_id"]
        == "ckpt-after"
    )
