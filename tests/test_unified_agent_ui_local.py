from __future__ import annotations

import base64
import importlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.in_memory import InMemorySessionService


class _UiRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                description="demo agent",
                type=SimpleNamespace(value="langgraph"),
            ),
            project_dir=".",
        )
        self.invocations: list[dict] = []
        self.run_server_calls: list[int] = []
        self.load_agent_calls = 0

    def load_agent(self) -> None:
        self.load_agent_calls += 1
        return None

    async def invoke(self, input_data: dict) -> dict:
        self.invocations.append(input_data)
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        self.invocations.append(input_data)
        yield {"type": "tool_call", "tool_name": "resume_lookup", "tool_args": {"keyword": "jd"}}
        yield {"type": "tool_result", "tool_name": "resume_lookup", "tool_output": '{"score": 91}'}
        yield {"type": "thinking", "delta": "plan"}
        yield {"type": "text", "delta": "hello"}
        yield {"type": "final", "output": "hello world"}

    def run_server(self, port: int = 8000) -> None:
        self.run_server_calls.append(port)


class _BrokenLoadRunner(_UiRunner):
    def load_agent(self) -> None:
        self.load_agent_calls += 1
        raise RuntimeError("runner load failed")


def _build_transport(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _UiRunner()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app)
    return server_app_module, runner, service, transport


@pytest.mark.asyncio
async def test_get_agent_ui_bootstrap_matches_local_shape_parity(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    _, runner, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent", "SessionId": "sess-bootstrap"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert set(payload["Data"].keys()) == {
        "Agent",
        "Modules",
        "Capabilities",
        "AccessMode",
        "SharePermissions",
        "ApiFormats",
        "Stream",
        "SessionId",
        "HostedRuntime",
        "Model",
    }
    assert payload["Data"]["Agent"]["AgentId"] == "demo-agent"
    assert payload["Data"]["Modules"] == ["Chat", "Build", "Deploy"]
    assert payload["Data"]["Capabilities"] == {
        "Attachments": True,
        "Thinking": True,
        "Approval": True,
        "StopRun": False,
        "ResumeRun": False,
        "MCP": False,
        "HostedRuntime": False,
    }
    assert payload["Data"]["AccessMode"] == "Owner"
    assert payload["Data"]["SharePermissions"] == {
        "Interactive": True,
        "DefaultPath": "/chat",
        "SharePath": "/chat",
    }
    assert payload["Data"]["ApiFormats"] == ["responses", "chat_completions"]
    assert payload["Data"]["Stream"] is True
    assert payload["Data"]["SessionId"] == "sess-bootstrap"
    assert payload["Data"]["HostedRuntime"] is None
    assert payload["Data"]["Model"]["id"] == "glm-5.1"
    assert payload["Data"]["Model"]["source"] == "OPENAI_MODEL_NAME"
    assert runner.load_agent_calls == 0


@pytest.mark.asyncio
async def test_list_agent_models_action_uses_real_current_model_without_gemini_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Data"]["Current"] == "glm-5.1"
    assert payload["Data"]["Source"] == "OPENAI_MODEL_NAME"
    assert [item["id"] for item in payload["Data"]["Models"]] == ["glm-5.1"]


@pytest.mark.asyncio
async def test_list_agent_models_action_matches_hosted_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert payload["Data"]["Current"] == "glm-5.1"
    assert payload["Data"]["Source"] == "OPENAI_MODEL_NAME"
    assert [item["id"] for item in payload["Data"]["Models"]] == ["glm-5.1"]


@pytest.mark.asyncio
async def test_run_agent_action_returns_responses_payload_and_persists_session(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "ApiFormat": "responses",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert payload["Data"]["object"] == "response"
    assert payload["Data"]["status"] == "completed"
    assert payload["Data"]["output_text"] == "assistant says hi"

    session_id = payload["Data"]["session_id"]
    session = await service.get_session(session_id)
    assert session is not None
    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent", "demo-agent", "demo-agent"]
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert runner.invocations[-1]["history"] == [{"role": "user", "content": "hello"}]
    assert runner.load_agent_calls == 1


@pytest.mark.asyncio
async def test_run_agent_action_normalizes_structured_text_and_inline_attachment(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)
    attachment_bytes = "候选人简历内容".encode("utf-8")
    attachment_b64 = base64.b64encode(attachment_bytes).decode("ascii")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请总结附件"},
                            {
                                "type": "input_file",
                                "inlineData": {
                                    "displayName": "resume.txt",
                                    "mimeType": "text/plain",
                                    "data": attachment_b64,
                                },
                            },
                        ],
                    }
                ],
                "ApiFormat": "responses",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    normalized_input = runner.invocations[-1]["input"]
    assert "请总结附件" in normalized_input
    assert "resume.txt" in normalized_input
    assert "候选人简历内容" in normalized_input
    assert runner.invocations[-1]["attachments"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "data": attachment_b64,
            "is_text": True,
            "size_bytes": len(attachment_bytes),
        }
    ]
    assert runner.invocations[-1]["attachment_results"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "file_uri": "",
            "size_bytes": len(attachment_bytes),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
            "text": "候选人简历内容",
        }
    ]

    session_id = payload["Data"]["session_id"]
    events = await service.get_events(session_id)
    assert events[0].content["parts"][0]["text"] == "请总结附件\n\n## 附件\n- resume.txt"
    assert "候选人简历内容" not in events[0].content["parts"][0]["text"]
    assert events[0].metadata["agent_input"] == normalized_input
    assert events[0].event_type == "user_message"


@pytest.mark.asyncio
async def test_run_agent_action_passes_binary_zip_attachment_to_runner(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)
    archive_bytes = b"PK\x03\x04demo-zip"
    archive_b64 = base64.b64encode(archive_bytes).decode("ascii")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "分析这个压缩包"},
                            {
                                "type": "input_file",
                                "inlineData": {
                                    "displayName": "bundle.zip",
                                    "mimeType": "application/zip",
                                    "data": archive_b64,
                                },
                            },
                        ],
                    }
                ],
                "ApiFormat": "responses",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    normalized_input = runner.invocations[-1]["input"]
    assert "bundle.zip" in normalized_input
    assert "ZIP 压缩包无法打开" in normalized_input
    assert runner.invocations[-1]["attachments"] == [
        {
            "display_name": "bundle.zip",
            "mime_type": "application/zip",
            "transport": "inline",
            "data": archive_b64,
            "is_text": False,
            "size_bytes": len(archive_bytes),
        }
    ]
    assert runner.invocations[-1]["attachment_results"] == [
        {
            "display_name": "bundle.zip",
            "mime_type": "application/zip",
            "transport": "inline",
            "file_uri": "",
            "size_bytes": len(archive_bytes),
            "kind": "archive",
            "status": "failed",
            "warnings": ["ZIP 压缩包无法打开，请确认文件未损坏后重试。"],
            "extraction_method": "zip_enumeration",
            "text_excerpt": "",
        }
    ]


@pytest.mark.asyncio
async def test_upload_file_action_returns_server_handle_and_stores_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("resume.txt", b"hello", "text/plain")},
        )

    assert response.status_code == 200
    payload = response.json()
    file_data = payload["Data"]["FileData"]
    assert file_data["fileUri"].startswith("ksadk-upload://")
    assert file_data["displayName"] == "resume.txt"
    assert file_data["mimeType"] == "text/plain"
    assert file_data["sizeBytes"] == 5

    file_id = file_data["fileUri"].removeprefix("ksadk-upload://")
    stored_files = list((tmp_path / ".agentengine" / "ui" / "files").glob(f"{file_id}*"))
    assert len(stored_files) == 1
    assert stored_files[0].read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_run_agent_action_normalizes_uploaded_file_handle_and_persists_compact_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    _, runner, service, transport = _build_transport(monkeypatch)
    attachment_bytes = "候选人简历内容".encode("utf-8")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        upload_response = await client.post(
            "/agentengine/api/v1/UploadFile",
            files={"file": ("resume.txt", attachment_bytes, "text/plain")},
        )
        uploaded = upload_response.json()["Data"]["FileData"]

        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请总结附件"},
                            {
                                "type": "input_file",
                                "fileData": uploaded,
                            },
                        ],
                    }
                ],
                "ApiFormat": "responses",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    normalized_input = runner.invocations[-1]["input"]
    assert "请总结附件" in normalized_input
    assert "resume.txt" in normalized_input
    assert "候选人简历内容" in normalized_input

    attachment = runner.invocations[-1]["attachments"][0]
    assert attachment["display_name"] == "resume.txt"
    assert attachment["mime_type"] == "text/plain"
    assert attachment["transport"] == "reference"
    assert attachment["file_uri"] == uploaded["fileUri"]
    assert attachment["size_bytes"] == len(attachment_bytes)
    assert attachment["is_text"] is True
    assert attachment["storage_path"].endswith(".txt")
    assert "data" not in attachment
    assert runner.invocations[-1]["attachment_results"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "reference",
            "file_uri": uploaded["fileUri"],
            "size_bytes": len(attachment_bytes),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
            "text": "候选人简历内容",
        }
    ]

    session_id = response.json()["Data"]["session_id"]
    events = await service.get_events(session_id)
    assert events[0].content["parts"][0]["text"] == "请总结附件\n\n## 附件\n- resume.txt"
    assert events[0].metadata["attachments"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "reference",
            "size_bytes": len(attachment_bytes),
            "is_text": True,
            "file_uri": uploaded["fileUri"],
        }
    ]
    assert "storage_path" not in events[0].metadata["attachments"][0]
    assert events[0].event_type == "user_message"


@pytest.mark.asyncio
async def test_run_agent_action_long_history_generates_semantic_checkpoint(monkeypatch):
    server_app_module, runner, service, transport = _build_transport(monkeypatch)
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    model_context_module = importlib.import_module("ksadk.conversations.model_context")

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user",
        session_id="sess-ui-semantic",
    )
    for turn_index in range(3):
        invocation_id = f"ui-sem-{turn_index}"
        user_text = f"长历史用户消息 {turn_index} " + ("甲方要求很多 " * 10)
        assistant_text = f"长历史助手回复 {turn_index} " + ("当前已经分析过 " * 10)
        await conversation_runtime.append_conversation_event(
            session_id=session.id,
            author="user",
            role="user",
            text=user_text,
            invocation_id=invocation_id,
            event_type="user_message",
            metadata={"agent_input": user_text},
            session_service_provider=lambda: service,
        )
        await conversation_runtime.append_conversation_event(
            session_id=session.id,
            author="demo-agent",
            role="model",
            text=assistant_text,
            invocation_id=invocation_id,
            event_type="assistant_message",
            session_service_provider=lambda: service,
        )

    class _SemanticSummaryClient:
        is_available = True

        async def summarize(self, *, model, messages, timeout_ms):
            assert model == "glm-5.1"
            assert timeout_ms > 0
            assert any("当前用户目标" in item["content"] for item in messages)
            return (
                "<analysis>draft</analysis><summary>当前用户目标\n- 继续处理默认 UI 长会话\n\n关键约束与偏好\n- 摘要质量优先\n\n已完成进展\n- 已为较早轮次生成 checkpoint\n\n重要决策/代码上下文\n- 仍然保留 append-only transcript\n\n未完成事项\n- 继续回答用户追问\n\n下一步工作位置\n- /agentengine/api/v1/RunAgent</summary>",
                {"prompt_tokens": 88, "completion_tokens": 22, "total_tokens": 110},
            )

    monkeypatch.setattr(
        "ksadk.conversations.semantic_summary.resolve_summary_model_client",
        lambda: _SemanticSummaryClient(),
    )
    monkeypatch.setattr(conversation_runtime, "AUTOCOMPACT_KEEP_TAIL_GROUPS", 1)
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 40)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 2)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "继续基于之前内容给结论"}],
                "SessionId": session.id,
                "Model": "glm-5.1",
                "ApiFormat": "responses",
                "Stream": False,
            },
        )
        events_response = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={"SessionId": session.id},
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["history"][0]["role"] == "model"
    assert "当前用户目标" in runner.invocations[-1]["history"][0]["content"]
    event_items = events_response.json()["Data"]["Events"]
    checkpoint = next(item for item in event_items if item["EventType"] == "context_checkpoint")
    assert checkpoint["Metadata"]["summary_strategy"] == "semantic"
    assert checkpoint["Metadata"]["summary_version"] == "v1"
    assert checkpoint["Metadata"]["summary_model"] == "glm-5.1"
    assert checkpoint["Metadata"]["summary_usage"]["total_tokens"] == 110
    assert "当前用户目标" in checkpoint["Content"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_session_kop_actions_crud_and_event_listing(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        session_id = created.json()["Data"]["Session"]["SessionId"]

        await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "SessionId": session_id,
                "ApiFormat": "responses",
            },
        )

        listed = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        fetched = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": session_id},
        )
        events = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={"SessionId": session_id},
        )
        deleted = await client.post(
            "/agentengine/api/v1/DeleteSession",
            json={"SessionId": session_id},
        )

    assert created.status_code == 200
    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert events.status_code == 200
    assert deleted.status_code == 200
    created_session = created.json()["Data"]["Session"]
    fetched_session = fetched.json()["Data"]["Session"]
    assert created_session["Title"] == ""
    assert created_session["Summary"] == ""
    assert created_session["FirstPrompt"] == ""
    assert created_session["LastPrompt"] == ""
    assert [item["SessionId"] for item in listed.json()["Data"]["Sessions"]] == [session_id]
    assert fetched_session["SessionId"] == session_id
    assert fetched_session["Title"] == "hello"
    assert fetched_session["TitleSource"] == "fallback_first_prompt"
    assert fetched_session["FirstPrompt"] == "hello"
    assert fetched_session["LastPrompt"] == "hello"
    assert fetched_session["Summary"] == "assistant says hi"
    assert [item["Author"] for item in events.json()["Data"]["Events"]] == [
        "user",
        "demo-agent",
        "demo-agent",
        "demo-agent",
    ]
    assert [item["EventType"] for item in events.json()["Data"]["Events"]] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert deleted.json()["Data"]["Deleted"] is True


@pytest.mark.asyncio
async def test_responses_endpoint_streams_thinking_and_text_events(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.tool_call" in lines
    assert "event: response.tool_result" in lines
    assert "event: response.reasoning.delta" in lines
    assert "event: response.output_text.delta" in lines
    assert "event: response.completed" in lines


@pytest.mark.asyncio
async def test_responses_endpoint_passes_attachment_results_to_runner(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)
    attachment_b64 = base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请分析附件"},
                            {
                                "type": "input_file",
                                "inlineData": {
                                    "displayName": "resume.txt",
                                    "mimeType": "text/plain",
                                    "data": attachment_b64,
                                },
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["attachment_results"] == [
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
async def test_streaming_run_agent_fails_before_starting_sse_when_runner_load_fails(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _BrokenLoadRunner()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "ApiFormat": "responses",
                "Stream": True,
            },
        )

    assert response.status_code == 500
    assert runner.load_agent_calls == 1


def test_cmd_web_launches_unified_local_server(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value="langgraph"),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.runners.unified_runner.UnifiedRunner.create",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert fake_runner.load_agent_calls == 0
    assert "Chainlit" not in result.output


def test_cmd_web_defaults_adk_stm_to_persistent_sqlite(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-adk-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value="adk"),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_STM_PATH", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_PATH", raising=False)
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.delenv("KSADK_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.runners.unified_runner.UnifiedRunner.create",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert os.environ["KSADK_STM_BACKEND"] == "sqlite"
    assert os.environ["KSADK_STM_PATH"] == str(
        project_dir / ".agentengine" / "ui" / "sessions.sqlite"
    )


def test_cmd_web_preserves_explicit_adk_stm_configuration(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-adk-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value="adk"),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.setenv("KSADK_STM_BACKEND", "local")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/custom-sessions.db")
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.delenv("KSADK_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.runners.unified_runner.UnifiedRunner.create",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert os.environ["KSADK_STM_BACKEND"] == "local"
    assert os.environ["KSADK_STM_PATH"] == "/tmp/custom-sessions.db"


@pytest.mark.asyncio
async def test_static_routes_serve_unified_agent_ui_shell(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        root_response = await client.get("/")
        chat_response = await client.get("/chat")
        script_match = re.search(r'src="(\./assets/[^"]+\.js)"', root_response.text)
        style_match = re.search(r'href="(\./assets/[^"]+\.css)"', root_response.text)
        assert script_match is not None
        assert style_match is not None
        js_response = await client.get(script_match.group(1).removeprefix("."))
        css_response = await client.get(style_match.group(1).removeprefix("."))

    assert root_response.status_code == 200
    assert chat_response.status_code == 200
    assert js_response.status_code == 200
    assert css_response.status_code == 200
    assert '<div id="root"></div>' in root_response.text
    assert '<div id="root"></div>' in chat_response.text
    assert root_response.text == chat_response.text
    assert 'type="module" crossorigin src="./assets/index-' in root_response.text
    assert 'rel="stylesheet" crossorigin href="./assets/index-' in root_response.text
    assert "/agentengine/api/v1/AttachmentContent" in js_response.text
    assert "/agentengine/api/v1/UploadFile" in js_response.text
    assert "/agentengine/api/v1/ListSessionEvents" in js_response.text
    assert "/agentengine/api/v1/ListAgentModels" in js_response.text
    assert "/agentengine/api/v1/RunAgent" in js_response.text
    assert "/run_sse" not in js_response.text
    assert "/agentengine/api/v1/models" not in js_response.text
    assert "overflow" in css_response.text


def test_web_ui_source_uses_title_and_summary_in_sidebar():
    app_source = Path("ksadk/server/web-ui/src/App.tsx").read_text(encoding="utf-8")
    sidebar_source = Path("ksadk/server/web-ui/src/components/chat/ChatSidebar.tsx").read_text(
        encoding="utf-8"
    )
    assert "session.Title" in app_source
    assert "session.Summary" in sidebar_source
    assert "session.SessionId.slice(0, 12)" not in sidebar_source


def test_web_ui_source_supports_clipboard_file_paste():
    source = Path("ksadk/server/web-ui/src/App.tsx").read_text(encoding="utf-8")
    assert "clipboardData.items" in source
    assert "onPaste" in source
    assert "getAsFile" in source


def test_web_ui_source_uses_adaptive_image_preview_sizing():
    app_source = Path("ksadk/server/web-ui/src/App.tsx").read_text(encoding="utf-8")
    preview_source = Path(
        "ksadk/server/web-ui/src/components/chat/AttachmentPreview.tsx"
    ).read_text(encoding="utf-8")
    assert "naturalWidth" in preview_source
    assert "naturalHeight" in preview_source
    assert "setPreviewImageSize" in app_source
