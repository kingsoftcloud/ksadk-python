from __future__ import annotations

import base64
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest, InlineData, Part
from ksadk.sessions.in_memory import InMemorySessionService


class _DummyRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                type=SimpleNamespace(value="mock"),
            ),
            project_dir=".",
        )
        self.calls: list[dict] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        yield {"type": "final", "output": "assistant says hi"}


class _OverrideStreamingRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                type=SimpleNamespace(value="mock"),
            ),
            project_dir=".",
        )

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        return {"output": "goodbye"}

    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "hel"}
        yield {"type": "text", "delta": "lo"}
        yield {"type": "final", "output": "goodbye"}


class _ModelAwareRunner(_DummyRunner):
    def __init__(self):
        super().__init__()
        self.prepared_models: list[str | None] = []

    def prepare_for_request(self, model: str | None) -> None:
        self.prepared_models.append(model)


class _ExternalModelsAsyncClient:
    """给 ListAgentModels 用的外部模型目录假客户端。"""

    def __init__(self, *args, payload=None, error: Exception | None = None, **kwargs):
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str, headers: dict | None = None):
        if self._error is not None:
            raise self._error
        request = httpx.Request("GET", url, headers=headers)
        return httpx.Response(200, json=self._payload, request=request)


def _sse_payloads(response_text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def _sse_events(response_text: str) -> list[tuple[str, dict]]:
    current_event = "message"
    events: list[tuple[str, dict]] = []
    for line in response_text.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ").strip() or "message"
            continue
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ").strip()
        if not payload or payload == "[DONE]":
            current_event = "message"
            continue
        events.append((current_event, json.loads(payload)))
        current_event = "message"
    return events


@pytest.mark.asyncio
async def test_run_sse_uses_new_session_service(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=False,
                stateDelta={"topic": "billing"},
            ).model_dump(),
        )

    assert response.status_code == 200
    first_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    payload = json.loads(first_line.removeprefix("data: "))
    session_id = payload["sessionId"]

    session = await service.get_session(session_id)
    assert session is not None
    assert session.state == {"topic": "billing"}

    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent", "demo-agent", "demo-agent"]
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert events[0].content["parts"][0]["text"] == "hello"
    assert events[2].content["parts"][0]["text"] == "assistant says hi"
    assert events[0].metadata["agent_input"] == "hello"

    assert runner.calls == [
        {
            "session_id": session_id,
            "input": "hello",
            "history": [{"role": "user", "content": "hello"}],
            "input_parts": [{"text": "hello"}],
            "attachments": [],
            "attachment_results": [],
            "model": None,
        }
    ]


@pytest.mark.asyncio
async def test_run_sse_passes_attachment_results_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={
                    "role": "user",
                    "parts": [
                        {"text": "请分析附件"},
                        Part(
                            inlineData=InlineData(
                                displayName="resume.txt",
                                mimeType="text/plain",
                                data=base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii"),
                            )
                        ).model_dump(exclude_none=True),
                    ],
                },
                streaming=False,
            ).model_dump(),
        )

    assert response.status_code == 200
    assert runner.calls[-1]["attachment_results"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "file_uri": "",
            "size_bytes": len("候选人简历内容".encode("utf-8")),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
            "text": "候选人简历内容",
        }
    ]


@pytest.mark.asyncio
async def test_create_session_rejects_explicit_session_owned_by_other_agent_or_user(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="other-agent",
        user_id="other-user",
        session_id="shared-session",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/apps/demo-agent/users/user-1/sessions",
            json={"sessionId": "shared-session"},
        )

    assert response.status_code == 409
    assert "different agent or user" in response.json()["detail"]


@pytest.mark.asyncio
async def test_run_sse_rejects_explicit_session_owned_by_other_agent_or_user(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()
    await service.create_session(
        agent_id="other-agent",
        user_id="other-user",
        session_id="shared-session",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId="shared-session",
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=False,
            ).model_dump(),
        )

    assert response.status_code == 409
    assert "different agent or user" in response.json()["detail"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_attachment_content_route_serves_uploaded_binary(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))
    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        upload_response = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("arch.png", b"\x89PNG\r\n\x1a\nbinary", "image/png")},
        )

        assert upload_response.status_code == 200
        file_uri = upload_response.json()["Data"]["FileData"]["fileUri"]

        content_response = await client.get(
            "/agentengine/api/v1/AttachmentContent",
            params={"FileUri": file_uri},
        )

    assert content_response.status_code == 200
    assert content_response.headers["content-type"].startswith("image/png")
    assert content_response.content == b"\x89PNG\r\n\x1a\nbinary"


@pytest.mark.asyncio
async def test_list_sessions_projects_heuristic_title_for_existing_fallback_session(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    created = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-heuristic-read",
    )
    await service.update_session_metadata(
        created.id,
        title="你好，请介绍一下你自己",
        title_source="fallback_first_prompt",
        first_prompt="你好，请介绍一下你自己",
        summary="你好！我是企业高端招聘全流程助手，可以协助你完成职位分析、候选人筛选和面试建议生成。",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )

    assert response.status_code == 200
    session = response.json()["Data"]["Sessions"][0]
    assert session["Title"] == "招聘助手能力"
    assert session["TitleSource"] == "heuristic"


@pytest.mark.asyncio
async def test_run_sse_stream_emits_authoritative_final_event_when_output_overrides_partials(
    monkeypatch,
):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _OverrideStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=True,
            ).model_dump(),
        )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert [payload["content"]["parts"][0]["text"] for payload in payloads] == [
        "hel",
        "lo",
        "goodbye",
    ]
    assert payloads[0]["partial"] is True
    assert payloads[1]["partial"] is True
    assert "partial" not in payloads[2]

    session_id = payloads[0]["sessionId"]
    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent", "demo-agent", "demo-agent"]
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert events[-2].content["parts"][0]["text"] == "goodbye"


@pytest.mark.asyncio
async def test_run_sse_stream_emits_compaction_status_events(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    runner = _OverrideStreamingRunner()
    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="session-with-history",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    monkeypatch.setattr(conversation_runtime, "AUTOCOMPACT_KEEP_TAIL_GROUPS", 1)
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 30)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 2)

    for turn_index in range(2):
        invocation_id = f"seed-{turn_index}"
        seed_text = f"历史消息 {turn_index} " + ("很长 " * 12)
        await conversation_runtime.append_conversation_event(
            session_id=session.id,
            author="user",
            role="user",
            text=seed_text,
            invocation_id=invocation_id,
            event_type="user_message",
            session_service_provider=lambda: service,
            metadata={"agent_input": seed_text},
        )
        await conversation_runtime.append_conversation_event(
            session_id=session.id,
            author="demo-agent",
            role="model",
            text=f"历史回复 {turn_index} " + ("继续 " * 12),
            invocation_id=invocation_id,
            event_type="assistant_message",
            session_service_provider=lambda: service,
        )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=session.id,
                newMessage={"role": "user", "parts": [{"text": "请继续基于历史回答"}]},
                streaming=True,
            ).model_dump(),
        )

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_names = [event_name for event_name, _ in events]
    assert event_names[:2] == [
        "response.compaction.start",
        "response.compaction.done",
    ]
    assert event_names.count("message") >= 2

    persisted_events = await service.get_events(session.id)
    assert [event.event_type for event in persisted_events] == [
        "user_message",
        "assistant_message",
        "user_message",
        "assistant_message",
        "user_message",
        "compaction_boundary",
        "context_checkpoint",
        "run_status",
        "assistant_message",
        "run_status",
    ]


@pytest.mark.asyncio
async def test_run_sse_prepares_runner_model_and_forwards_model_to_invoke(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _ModelAwareRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=False,
                model="gpt-4o",
            ).model_dump(),
        )

    assert response.status_code == 200
    assert runner.prepared_models == ["gpt-4o"]
    assert runner.calls[-1]["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_chat_completions_forwards_model_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _ModelAwareRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "model": "glm-5",
            },
        )

    assert response.status_code == 200
    assert runner.prepared_models == ["glm-5"]
    assert runner.calls[-1]["model"] == "glm-5"


@pytest.mark.asyncio
async def test_chat_completions_passes_attachment_results_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    attachment_b64 = base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii")
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"text": "请分析附件"},
                            {
                                "inlineData": {
                                    "displayName": "resume.txt",
                                    "mimeType": "text/plain",
                                    "data": attachment_b64,
                                }
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.calls[-1]["attachment_results"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "file_uri": "",
            "size_bytes": len("候选人简历内容".encode("utf-8")),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
            "text": "候选人简历内容",
        }
    ]


@pytest.mark.asyncio
async def test_chat_completions_reuses_prior_attachment_results_on_follow_up_turn(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    attachment_b64 = base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii")
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first_response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"text": "请分析附件"},
                            {
                                "inlineData": {
                                    "displayName": "resume.txt",
                                    "mimeType": "text/plain",
                                    "data": attachment_b64,
                                }
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )
        first_payload = first_response.json()
        session_id = first_payload["session_id"]

        second_response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "继续分析"}],
                "session_id": session_id,
                "stream": False,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert runner.calls[-1]["attachment_results"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "file_uri": "",
            "size_bytes": len("候选人简历内容".encode("utf-8")),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
            "text": "候选人简历内容",
        }
    ]


@pytest.mark.asyncio
async def test_list_agent_models_action_normalizes_default_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    real_async_client = httpx.AsyncClient
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={"data": [{"id": "glm-5"}]},
            **kwargs,
        ),
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with real_async_client(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["Current"] == "glm-5"
    assert payload["Models"] == [
        {
            "id": "glm-5",
            "display_name": "glm-5",
            "context_window_tokens": 200000,
            "max_output_tokens": 32000,
            "auto_compact_threshold_tokens": 167000,
            "auto_compact_threshold_percentage": 84,
            "capabilities": {
                "function_calling": True,
                "structured_output": True,
                "context_caching": True,
            },
            "limits": {
                "context_window_tokens": 200000,
                "max_input_tokens": 200000,
                "max_output_tokens": 32000,
                "max_reasoning_tokens": 32000,
                "rpm": 500,
                "tpm": 1000000,
            },
            "pricing": {
                "online_input_per_million": 4.0,
                "online_output_per_million": 18.0,
                "batch_input_per_million": 2.0,
                "batch_output_per_million": 9.0,
                "online_cache_hit_input_per_million": 1.0,
                "batch_cache_hit_input_per_million": 1.0,
            },
        }
    ]


@pytest.mark.asyncio
async def test_list_agent_models_action_preserves_upstream_fields_and_normalizes_aliases(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    real_async_client = httpx.AsyncClient
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "kimi-k2.5")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={
                "data": [
                    {
                        "id": "kimi-k2.5",
                        "owned_by": "ksyun",
                        "context_length": 131072,
                        "max_tokens": 4096,
                    }
                ]
            },
            **kwargs,
        ),
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with real_async_client(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    item = response.json()["Data"]["Models"][0]
    assert item["id"] == "kimi-k2.5"
    assert item["owned_by"] == "ksyun"
    assert item["context_length"] == 131072
    assert item["max_tokens"] == 4096
    assert item["context_window_tokens"] == 131072
    assert item["max_output_tokens"] == 4096
    assert item["limits"]["context_window_tokens"] == 131072
    assert item["limits"]["max_output_tokens"] == 4096


@pytest.mark.asyncio
async def test_list_agent_models_action_without_api_base_returns_default_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5")

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["Current"] == "glm-5"
    assert [item["id"] for item in payload["Models"]] == ["glm-5"]
    assert payload["Models"][0]["context_window_tokens"] == 200000
    assert payload["Models"][0]["limits"]["max_output_tokens"] == 32000


@pytest.mark.asyncio
async def test_legacy_models_routes_are_not_exposed(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    transport = httpx.ASGITransport(app=server_app_module.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        get_response = await client.get("/agentengine/api/v1/models")
        post_response = await client.post("/agentengine/api/v1/models", json={"model": "glm-5"})

    assert get_response.status_code == 404
    assert post_response.status_code in {404, 405}
