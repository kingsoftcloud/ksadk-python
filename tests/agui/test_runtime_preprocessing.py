from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.runtime_context import get_current_invocation_context
from ksadk.sessions import InMemorySessionService


class _CapturingRunner:
    def __init__(self) -> None:
        self.detection_result = SimpleNamespace(
            name="preprocessing-fixture",
            type=SimpleNamespace(value="langgraph"),
        )
        self.prepared_models: list[str | None] = []
        self.inputs: list[dict[str, Any]] = []
        self.contexts: list[Any] = []

    def prepare_for_request(self, model: str | None) -> None:
        self.prepared_models.append(model)

    async def stream(self, input_data: dict[str, Any]):
        self.inputs.append(input_data)
        self.contexts.append(get_current_invocation_context())
        yield {
            "type": "final",
            "output": "done",
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }


@pytest.mark.asyncio
async def test_conversation_request_reuses_full_runtime_preprocessing(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr(
        "ksadk.conversations.runtime_preparation.resolve_session_service", lambda: service
    )
    monkeypatch.setattr(
        "ksadk.runtime.preprocessing._build_runner_ambient_contexts",
        lambda **_kwargs: {
            "kb_context": {"formatted_text": "tenant handbook"},
            "memory_context": {"formatted_text": "prefers concise answers"},
        },
    )

    spans: list[Any] = []

    class _Span:
        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    @asynccontextmanager
    async def _span_scope(_name: str):
        span = _Span()
        spans.append(span)
        yield span

    monkeypatch.setattr("ksadk.runtime.runner_adapter._conversation_span_scope", _span_scope)

    runner = _CapturingRunner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    request = StartRequest(
        input="fallback must not replace canonical messages",
        user_id="user-1",
        session_id="thread-1",
        agent_id="agent-1",
        model="model-1",
        config={"ag-ui": {"inject_a2ui_tool": True}},
        metadata={
            "invocation_id": "run-1",
            "transport": "ag-ui",
            "conversation_request": {
                "messages": [
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "inspect image"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,aGVsbG8=",
                            },
                        ],
                    },
                ],
                "model_metadata": {"id": "model-1", "context_length": 8192},
                "model_options": {"reasoning": {"enabled": False}},
                "state_delta": {"workspace": "demo"},
                "instructions": "Follow tenant policy.",
                "request_metadata": {"safety_identifier": "safe-user"},
                "custom_metadata": {"source": "hosted-ui"},
                "account_id": "account-1",
                "response_id": "agui-run-1",
            },
        },
    )

    handle = await adapter.start(request)
    events = [event async for event in adapter.stream(handle)]

    assert runner.prepared_models == ["model-1"]
    assert events[-1].event_type == "run.completed"
    payload = runner.inputs[0]
    assert payload["input"].startswith("inspect image\n\n[上传文件: uploaded_image")
    assert payload["history"][:2] == [
        {"role": "user", "content": "old question"},
        {"role": "model", "content": "old answer"},
    ]
    assert payload["history"][2]["role"] == "user"
    assert payload["history"][2]["content"] == payload["input"]
    assert payload["has_current_files"] is True
    assert payload["current_attachments"][0]["mime_type"] == "image/png"
    assert payload["model"] == "model-1"
    assert payload["model_metadata"]["context_length"] == 8192
    assert payload["model_options"]["reasoning"]["enabled"] is False
    assert payload["instructions"] == "Follow tenant policy."
    assert payload["ag-ui"] == {"inject_a2ui_tool": True}
    assert payload["kb_context"] == {"formatted_text": "tenant handbook"}
    assert payload["memory_context"] == {"formatted_text": "prefers concise answers"}

    platform_context = payload["platform_context"]
    assert platform_context["agent_id"] == "agent-1"
    assert platform_context["user_id"] == "user-1"
    assert platform_context["account_id"] == "account-1"
    assert platform_context["session_id"] == "thread-1"
    assert platform_context["metadata"] == {"source": "hosted-ui"}
    assert runner.contexts[0] is not None
    assert runner.contexts[0].to_payload() == platform_context
    assert get_current_invocation_context() is None

    stored_session = await service.get_session("thread-1")
    assert stored_session is not None
    assert stored_session.user_id == "user-1"
    assert stored_session.state["workspace"] == "demo"
    stored_events = await service.get_events("thread-1")
    assert stored_events[-1].metadata["runtime_metadata"] == {
        "invocation_id": "run-1",
        "transport": "ag-ui",
        "safety_identifier": "safe-user",
    }

    assert spans[0].attributes["ksadk.agent_id"] == "agent-1"
    assert spans[0].attributes["ksadk.user_id"] == "user-1"
    assert spans[0].attributes["ksadk.session_id"] == "thread-1"
    assert spans[0].attributes["ksadk.invocation_id"] == "run-1"
    assert spans[0].attributes["ksadk.response_id"] == "agui-run-1"
    assert spans[0].attributes["gen_ai.request.model"] == "model-1"
    assert spans[0].attributes["gen_ai.prompt"].startswith("inspect image [上传文件:")
    assert spans[0].attributes["gen_ai.completion"] == "done"
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 4
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 2


@pytest.mark.asyncio
async def test_start_without_conversation_request_keeps_frozen_runner_payload():
    runner = _CapturingRunner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="fixture")
    handle = await adapter.start(
        StartRequest(
            input={"custom": "state"},
            user_id="user-1",
            session_id="session-1",
            config={"native": True},
            metadata={"invocation_id": "run-legacy", "transport": "a2a"},
        )
    )

    _ = [event async for event in adapter.stream(handle)]

    assert runner.prepared_models == []
    assert runner.inputs == [
        {
            "native": True,
            "input": {"custom": "state"},
            "session_id": "session-1",
            "invocation_id": "run-legacy",
            "metadata": {"invocation_id": "run-legacy", "transport": "a2a"},
        }
    ]
