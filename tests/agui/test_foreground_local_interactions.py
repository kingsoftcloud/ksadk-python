"""Foreground compatibility inputs must preserve preparation and native resume."""

import asyncio

import pytest

from ksadk.kernel.state import RunState
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from tests.agui.test_default_local_interactions import local_app
from tests.agui.test_kernel_native_input import body, until


async def test_foreground_attachment_options_and_original_turn_survive_native_resume(
    tmp_path, monkeypatch,
):
    before, tools, requests = [], [], []
    ocr_calls = []

    def fixture_ocr(raw, mime_type, display_name):
        ocr_calls.append((raw, mime_type, display_name))
        return {"text": "fixture image text", "engine": "fixture-ocr"}

    # This case verifies attachment preparation and native resume, not OCR.
    # Keep the real image normalization path without loading optional models
    # for the synthetic five-byte image, especially on cold parallel workers.
    monkeypatch.setattr("ksadk.conversations.attachments.perform_ocr", fixture_ocr)
    original = LangGraphRuntimeAdapter.start

    async def capture(self, request):
        handle = await original(self, request)
        requests.append(request)
        return handle

    monkeypatch.setattr(LangGraphRuntimeAdapter, "start", capture)
    # This fixture installs a compiled graph directly and has no entry file to
    # reload for a model override. Observe selection at the runner boundary.
    models = []
    monkeypatch.setattr(
        LangGraphRunner, "prepare_for_request", lambda self, model: models.append(model)
    )
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        async with asyncio.timeout(10):
            response = await s.client.post("/agentengine/api/v1/RunAgent", json={
                "AgentId": "agent", "SessionId": "thread-1", "InvocationId": "attachment-turn",
                "ApiFormat": "responses", "Stream": True, "Model": "test-model",
                "ModelMetadata": {"id": "test-model", "context_length": 8192},
                "ModelOptions": {"reasoning": {"enabled": False}},
                "Metadata": {"source": "attachment-test", "agentengine": {
                    "tool_approval_mode": "risk",
                }},
                "ResponsesInput": [{"role": "user", "content": [
                    {"type": "input_text", "text": "inspect this image"},
                    {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
                ]}],
            })
        assert response.status_code == 200, response.text
        assert '"interaction.requested"' in response.text
        assert "internal-context-marker" not in response.text
        assert len(requests) == 1 and requests[0].user_id == "user-1"
        prepared = requests[0].metadata["conversation_request"]["prepared_turn"]
        assert requests[0].model == "test-model"
        assert models == ["test-model"]
        assert prepared["model_options"]["reasoning"]["enabled"] is False
        assert prepared["has_current_files"]
        assert prepared["current_attachments"][0]["mime_type"] == "image/png"
        assert prepared["current_attachment_results"][0]["text_excerpt"] == "fixture image text"
        assert ocr_calls == [(b"hello", "image/png", "uploaded_image")]
        runtime = s.app.state.runtime.local_interactions.runtime
        record = (await runtime.kernel_store.list_pending_interactions("local", "thread-1"))[0]
        assert before == [1] and not tools
        answered = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, 0)
        )
        assert answered.status_code == 202, answered.text

        async def completed():
            return (await runtime.kernel_store.load_run(record.run_id)).state is RunState.COMPLETED

        await until(completed)
        assert before == [1] and tools == [0] and len(requests) == 1
        assert len(ocr_calls) == 1
        user_echoes = [event for event in await s.service.get_events("thread-1")
                       if event.event_type == "user_message"]
        assert len(user_echoes) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_pending_foreground_input_returns_instead_of_waiting_for_a_second_request(
    tmp_path, monkeypatch, stream,
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        async with asyncio.timeout(8):
            response = await s.client.post("/agentengine/api/v1/RunAgent", json={
                "AgentId": "agent", "SessionId": "thread-1", "InvocationId": "pending-turn",
                "Stream": stream, "Messages": [{"role": "user", "content": "request input"}],
            })
        assert response.status_code == (200 if stream else 202), response.text
        if stream:
            assert '"interaction.requested"' in response.text
        else:
            data = response.json()["Data"]
            assert data["status"] == "incomplete"
            assert data["interaction"]["kind"] == "structured_input"
            assert "native_target" not in data["interaction"]
        assert before == [1] and not tools
