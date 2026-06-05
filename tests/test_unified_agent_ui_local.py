from __future__ import annotations

import base64
import importlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.base import SessionEvent
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
        yield {
            "type": "responses_output",
            "response_id": "resp_demo",
            "output": [
                {
                    "id": "fc_demo",
                    "type": "function_call",
                    "name": "resume_lookup",
                    "arguments": '{"keyword":"jd"}',
                }
            ],
        }
        yield {"type": "final", "output": "hello world"}

    def run_server(self, port: int = 8000) -> None:
        self.run_server_calls.append(port)


class _BrokenLoadRunner(_UiRunner):
    def load_agent(self) -> None:
        self.load_agent_calls += 1
        raise RuntimeError("runner load failed")


class _InterruptRunner(_UiRunner):
    async def stream(self, input_data: dict):
        self.invocations.append(input_data)
        yield {"type": "text", "delta": "need "}
        yield {
            "type": "interrupt",
            "interrupt_info": {"message": "确认执行?", "tool_name": "delete_file"},
        }


class _GenericInterruptRunner(_UiRunner):
    async def stream(self, input_data: dict):
        self.invocations.append(input_data)
        yield {
            "type": "interrupt",
            "interrupt_info": {"message": "需要人工确认"},
        }


class _FrameworkUiRunner(_UiRunner):
    def __init__(self, framework: str):
        super().__init__()
        self.detection_result.type = SimpleNamespace(value=framework)


class _KeyboardInterruptServerRunner(_UiRunner):
    def run_server(self, port: int = 8000) -> None:
        self.run_server_calls.append(port)
        raise KeyboardInterrupt


@pytest.fixture(autouse=True)
def _block_real_browser_open(monkeypatch):
    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.setattr(cmd_web_module.webbrowser, "open", lambda _url: None)


def _build_transport(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _UiRunner()
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app)
    return server_app_module, runner, service, transport


def _build_transport_with_runner(monkeypatch, runner):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app)
    return server_app_module, runner, service, transport


@pytest.fixture
def active_trace_provider():
    provider = TracerProvider()
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._set_tracer_provider(provider, log=False)
    yield
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


@pytest.mark.asyncio
async def test_get_agent_ui_bootstrap_matches_local_shape_parity(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.delenv("KSADK_TOOL_APPROVAL_MODE", raising=False)
    monkeypatch.delenv("KSADK_SANDBOX_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_SANDBOX_TEMPLATE_ID", raising=False)
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_SKILL_RUNTIME_TEMPLATE_ID", raising=False)
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
        "WorkspaceFiles",
        "AccessMode",
        "SharePermissions",
        "ApiFormats",
        "Stream",
        "SessionId",
        "SessionBackend",
        "HostedRuntime",
        "Model",
    }
    assert payload["Data"]["Agent"]["AgentId"] == "demo-agent"
    assert payload["Data"]["Agent"]["Framework"] == "langgraph"
    assert payload["Data"]["Modules"] == ["Chat", "Build", "Deploy"]
    assert payload["Data"]["Capabilities"] == {
        "Attachments": True,
        "WorkspaceFiles": True,
        "Thinking": True,
        "Approval": True,
        "StopRun": True,
        "ResumeRun": True,
        "MCP": False,
        "HostedRuntime": False,
        "NativeTerminal": {
            "Enabled": False,
            "Mode": None,
            "Protocol": "ks-terminal.v1",
            "Path": None,
        },
        "BuiltinTools": [
            {
                "name": "list_skills",
                "group": "skill",
                "description": "List skills discoverable from configured Skill Spaces.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
            },
            {
                "name": "search_skills",
                "group": "skill",
                "description": "Search skills by name, aliases, tags, description, and examples.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
            },
            {
                "name": "load_skill",
                "group": "skill",
                "description": "Download and load a skill's SKILL.md instructions from configured Skill Spaces.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": ["skill_cache_write"],
                "enabled": True,
            },
            {
                "name": "execute_skills",
                "group": "skill",
                "description": "Execute a workflow through the configured Skill Runtime.",
                "risk_level": "high",
                "requires_approval": False,
                "side_effects": ["isolated_runtime_execution"],
                "enabled": False,
                "backend": "disabled",
                "boundary": "isolated_skill_runtime",
            },
            {
                "name": "workspace_status",
                "group": "workspace",
                "description": "Return current AgentEngine workspace status.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "list_workspace_files",
                "group": "workspace",
                "description": "List files under the AgentEngine workspace.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "read_workspace_file",
                "group": "workspace",
                "description": "Read a UTF-8 text file from the AgentEngine workspace.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "write_workspace_file",
                "group": "workspace",
                "description": "Write a UTF-8 text file inside the AgentEngine workspace.",
                "risk_level": "medium",
                "requires_approval": False,
                "side_effects": ["workspace_write"],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "write_workspace_files",
                "group": "workspace",
                "description": "Write multiple UTF-8 text files inside the AgentEngine workspace.",
                "risk_level": "medium",
                "requires_approval": False,
                "side_effects": ["workspace_write"],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "search_workspace_files",
                "group": "workspace",
                "description": "Search UTF-8 text files in the AgentEngine workspace.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "delete_workspace_file",
                "group": "workspace",
                "description": "Delete a file or empty directory inside the AgentEngine workspace.",
                "risk_level": "high",
                "requires_approval": False,
                "side_effects": ["workspace_delete"],
                "enabled": True,
                "boundary": "workspace_root",
            },
            {
                "name": "component_status",
                "group": "platform",
                "description": "Report AgentEngine built-in toolset and runtime binding status.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
            },
            {
                "name": "sandbox_status",
                "group": "sandbox",
                "description": "Report configured AgentEngine sandbox status and boundaries.",
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": False,
                "backend": "none",
                "boundary": "isolated_sandbox",
            },
            {
                "name": "run_command",
                "group": "sandbox",
                "description": "Run a shell command inside the configured isolated sandbox.",
                "risk_level": "high",
                "requires_approval": False,
                "side_effects": ["sandbox_command_execution"],
                "enabled": False,
                "backend": "none",
                "boundary": "isolated_sandbox",
            },
            {
                "name": "run_code",
                "group": "sandbox",
                "description": "Write code to the sandbox and execute it through the configured sandbox backend.",
                "risk_level": "high",
                "requires_approval": False,
                "side_effects": ["sandbox_code_execution"],
                "enabled": False,
                "backend": "none",
                "boundary": "isolated_sandbox",
            },
        ],
        "RunLifecycle": {
            "Enabled": True,
            "Resume": True,
            "Abort": True,
        },
    }
    assert payload["Data"]["WorkspaceFiles"] == {
        "Enabled": True,
        "MaxUploadBytes": 104857600,
        "SupportsDelete": True,
        "RootLabel": "workspace",
        "EntryAction": "ListWorkspaceFiles",
        "UploadAction": "AddWorkspaceFile",
        "ContentPath": "/agentengine/api/v1/GetWorkspaceFileContent",
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
@pytest.mark.parametrize("framework", ["hermes", "openclaw"])
async def test_get_agent_ui_bootstrap_enables_tui_only_for_native_tui_frameworks(
    monkeypatch,
    framework,
):
    _, _, _, transport = _build_transport_with_runner(
        monkeypatch,
        _FrameworkUiRunner(framework),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": f"{framework}-agent"},
        )

    assert response.status_code == 200
    terminal = response.json()["Data"]["Capabilities"]["NativeTerminal"]
    assert terminal == {
        "Enabled": True,
        "Mode": "tui",
        "Protocol": "ks-terminal.v1",
        "Path": "/_ksadk/terminal/ws",
    }


@pytest.mark.asyncio
async def test_get_agent_ui_bootstrap_disables_tui_for_generic_frameworks(monkeypatch):
    _, _, _, transport = _build_transport_with_runner(
        monkeypatch,
        _FrameworkUiRunner("langgraph"),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "langgraph-agent"},
        )

    assert response.status_code == 200
    terminal = response.json()["Data"]["Capabilities"]["NativeTerminal"]
    assert terminal["Enabled"] is False
    assert terminal["Mode"] is None
    assert terminal["Path"] is None


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
async def test_run_agent_action_forwards_model_metadata_to_conversation_runtime(monkeypatch):
    server_app_module, _, _, transport = _build_transport(monkeypatch)
    captured: dict[str, object] = {}

    async def _fake_invoke_conversation_once(**kwargs):
        captured.update(kwargs)
        return "sess-model-metadata", {"output_text": "assistant says hi", "model": kwargs.get("model")}

    monkeypatch.setattr(server_app_module.conversation, "invoke_conversation_once", _fake_invoke_conversation_once)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "ApiFormat": "responses",
                "Stream": False,
                "Model": "glm-5.1",
                "ModelMetadata": {
                    "id": "glm-5.1",
                    "context_length": "64k",
                    "max_completion_tokens": "8k",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["Code"] == 0
    assert captured["model"] == "glm-5.1"
    assert captured["model_metadata"] == {
        "id": "glm-5.1",
        "context_length": "64k",
        "max_completion_tokens": "8k",
    }


@pytest.mark.asyncio
async def test_run_agent_action_streaming_responses_uses_responses_lifecycle(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-runagent-responses",
                "Messages": [{"role": "user", "content": "hello"}],
                "ApiFormat": "responses",
                "Stream": True,
                "Model": "glm-5.1",
            },
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.created" in lines
    assert "event: response.in_progress" in lines
    assert "event: response.output_item.added" in lines
    assert "event: response.function_call_arguments.delta" in lines
    assert "event: response.ksadk.tool_result" in lines
    assert "event: response.completed" in lines
    assert "event: response.tool_call" not in lines
    assert "event: response.tool_result" not in lines
    assert runner.invocations[-1]["model"] == "glm-5.1"
    assert runner.invocations[-1]["session_id"] == "sess-runagent-responses"
    assert await service.get_session("sess-runagent-responses") is not None
    stored_events = await service.get_events("sess-runagent-responses")
    assistant_events = [event for event in stored_events if event.event_type == "assistant_message"]
    assert assistant_events[-1].metadata["response_id"] == "resp_demo"
    assert assistant_events[-1].metadata["responses_output"][0]["type"] == "function_call"

    current_event = ""
    completed_payload = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: ") and current_event == "response.completed":
            completed_payload = json.loads(line.removeprefix("data: "))
    assert completed_payload is not None
    assert completed_payload["model"] == "glm-5.1"
    assert completed_payload["session_id"] == "sess-runagent-responses"


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
    assert events[0].content["parts"] == [
        {"type": "input_text", "text": "请总结附件"},
        {
            "type": "input_file",
            "filename": "resume.txt",
            "file_data": attachment_b64,
        },
    ]
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
    assert events[0].content["parts"] == [
        {"type": "input_text", "text": "请总结附件"},
        {
            "type": "input_file",
            "filename": "resume.txt",
            "file_url": uploaded["fileUri"],
        },
    ]
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
async def test_run_agent_action_uses_responses_input_for_normal_responses_run(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "ApiFormat": "responses",
                "Messages": [{"role": "user", "content": "SHOULD_NOT_USE"}],
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello from responses input"}],
                    }
                ],
                "Stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["input"] == "hello from responses input"
    assert runner.invocations[-1]["input_content"] == [
        {"type": "input_text", "text": "hello from responses input"}
    ]
    assert runner.invocations[-1]["input_messages"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "hello from responses input"}],
        }
    ]


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
    _, runner, service, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                "model": "glm-5.1",
                "session_id": "sess-responses-stream",
                "stream": True,
            },
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.created" in lines
    assert "event: response.in_progress" in lines
    assert "event: response.output_item.added" in lines
    assert "event: response.function_call_arguments.delta" in lines
    assert "event: response.function_call_arguments.done" in lines
    assert "event: response.ksadk.tool_result" in lines
    assert "event: response.reasoning.delta" in lines
    assert "event: response.output_text.delta" in lines
    assert "event: response.output_text.done" in lines
    assert "event: response.completed" in lines
    added_indexes = []
    current_event = ""
    for line in response.text.splitlines():
        if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
        elif line.startswith("data: ") and current_event == "response.output_item.added":
            added_indexes.append(json.loads(line.removeprefix("data: "))["output_index"])
    assert added_indexes == [0, 1, 2]
    assert runner.invocations[-1]["model"] == "glm-5.1"
    assert runner.invocations[-1]["session_id"] == "sess-responses-stream"
    assert await service.get_session("sess-responses-stream") is not None

    completed_payloads = []
    current_event = ""
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: ") and current_event == "response.completed":
            completed_payloads.append(json.loads(line.removeprefix("data: ")))
    assert completed_payloads[-1]["model"] == "glm-5.1"
    assert completed_payloads[-1]["session_id"] == "sess-responses-stream"


@pytest.mark.asyncio
async def test_responses_endpoint_passes_full_request_history_to_runner(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {"role": "user", "content": "写一个python快排的示例"},
                    {"role": "assistant", "content": "这是 Python 快速排序示例。"},
                    {"role": "user", "content": "用go"},
                ],
                "model": "glm-5.1",
                "session_id": "sess-responses-history",
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["input"] == "用go"
    assert runner.invocations[-1]["history"] == [
        {"role": "user", "content": "写一个python快排的示例"},
        {"role": "model", "content": "这是 Python 快速排序示例。"},
        {"role": "user", "content": "用go"},
    ]


@pytest.mark.asyncio
async def test_responses_endpoint_non_streaming_supports_instructions_and_metadata(
    monkeypatch,
    active_trace_provider,
):
    _, runner, service, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "instructions": "只用中文回答",
                "metadata": {"trace_label": "demo"},
                "stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["metadata"]["trace_label"] == "demo"
    assert payload["metadata"]["trace_id"]
    assert payload["metadata"]["root_span_id"]
    assert payload["output_text"] == "assistant says hi"
    assert payload["session_id"]
    assert runner.invocations[-1]["instructions"] == "只用中文回答"

    events = await service.get_events(payload["session_id"])
    user_event = next(event for event in events if event.event_type == "user_message")
    assert user_event.content["parts"][0]["text"] == "hello"
    assert user_event.metadata["instructions"] == "只用中文回答"
    assert user_event.metadata["request_metadata"] == {"trace_label": "demo"}


@pytest.mark.asyncio
async def test_responses_endpoint_streaming_interrupt_returns_incomplete(monkeypatch):
    _, _, service, transport = _build_transport_with_runner(monkeypatch, _InterruptRunner())

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={"input": "delete it", "stream": True},
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.output_item.added" in lines
    assert "event: response.incomplete" in lines
    assert "event: response.completed" not in lines

    data_lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert any(
        json.loads(line).get("item", {}).get("type") == "mcp_approval_request"
        for line in data_lines
    )
    incomplete_payload = next(
        json.loads(line)
        for line in data_lines
        if json.loads(line).get("status") == "incomplete"
    )
    assert incomplete_payload["incomplete_details"]["reason"] == "approval_required"
    events = await service.get_events(incomplete_payload["session_id"])
    assert any(event.event_type == "approval_request" for event in events)


@pytest.mark.asyncio
async def test_responses_endpoint_streaming_generic_interrupt_uses_ksadk_extension(monkeypatch):
    _, _, _, transport = _build_transport_with_runner(monkeypatch, _GenericInterruptRunner())

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={"input": "review it", "stream": True},
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.ksadk.approval_request" in lines
    assert "event: response.incomplete" in lines


@pytest.mark.asyncio
async def test_responses_endpoint_accepts_mcp_approval_response_resume(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)
    await service.create_session(agent_id="demo-agent", user_id="user", session_id="sess-approval")
    await service.append_event(
        "sess-approval",
        SessionEvent(
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "confirm tool"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_123",
                    "tool_name": "delete_file",
                    "arguments": {"path": "notes.txt"},
                }
            },
            invocation_id="inv-approval",
        ),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "session_id": "sess-approval",
                "previous_response_id": "resp_previous",
                "input": [
                    {
                        "type": "mcp_approval_response",
                        "id": "mcprsp_123",
                        "approval_request_id": "appr_123",
                        "approve": True,
                        "reason": "approved",
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["metadata"]["previous_response_id"] == "resp_previous"
    assert runner.invocations[-1]["resume"] is True
    assert runner.invocations[-1]["input"] == {
        "type": "mcp_approval_response",
        "id": "mcprsp_123",
        "approval_request_id": "appr_123",
        "approve": True,
        "reason": "approved",
        "tool_name": "delete_file",
        "tool_args": {
            "path": "notes.txt",
            "approval": {
                "approved": True,
                "approval_request_id": "appr_123",
                "reason": "approved",
            },
        },
        "approval": {
            "approved": True,
            "approval_request_id": "appr_123",
            "reason": "approved",
        },
    }
    events = await service.get_events("sess-approval")
    assert [event.event_type for event in events[:2]] == ["approval_request", "approval_response"]


@pytest.mark.asyncio
async def test_responses_endpoint_streams_mcp_approval_response_resume(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)
    await service.create_session(
        agent_id="demo-agent", user_id="user", session_id="sess-approval-stream"
    )
    await service.append_event(
        "sess-approval-stream",
        SessionEvent(
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "confirm tool"}]},
            metadata={
                "interrupt_info": {
                    "approval_request_id": "appr_stream",
                    "tool_name": "write_workspace_file",
                    "arguments": {"path": "notes.txt", "content": "hello"},
                    "run_id": "run_stream",
                }
            },
            invocation_id="inv-approval",
        ),
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "session_id": "sess-approval-stream",
                "previous_response_id": "resp_previous",
                "input": [
                    {
                        "type": "mcp_approval_response",
                        "approval_request_id": "appr_stream",
                        "approve": True,
                    }
                ],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "event: response.completed" in response.text
    assert runner.invocations[-1]["resume"] is True
    assert runner.invocations[-1]["input"] == {
        "type": "function_call_output",
        "call_id": "run_stream",
        "output": {
            "ok": True,
            "path": "notes.txt",
            "absolute_path": runner.invocations[-1]["input"]["output"]["absolute_path"],
            "size": 5,
        },
    }
    assert Path(runner.invocations[-1]["input"]["output"]["absolute_path"]).read_text(
        encoding="utf-8"
    ) == "hello"


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
async def test_responses_endpoint_maps_openai_input_image_to_current_attachments(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)
    image_b64 = base64.b64encode(b"\x89PNG\r\n").decode("ascii")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请分析这张图"},
                            {
                                "type": "input_image",
                                "image_url": f"data:image/png;base64,{image_b64}",
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["has_current_files"] is True
    assert runner.invocations[-1]["current_attachments"] == [
        {
            "display_name": "uploaded_image",
            "mime_type": "image/png",
            "transport": "inline",
            "data": image_b64,
            "is_text": False,
            "size_bytes": len(b"\x89PNG\r\n"),
        }
    ]
    assert runner.invocations[-1]["attachments"] == runner.invocations[-1]["current_attachments"]
    assert runner.invocations[-1]["input_content"] == [
        {"type": "input_text", "text": "请分析这张图"},
        {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
    ]
    assert runner.invocations[-1]["input_parts"][1] == {
        "inlineData": {
            "data": image_b64,
            "mimeType": "image/png",
            "displayName": "uploaded_image",
        }
    }


@pytest.mark.asyncio
async def test_responses_endpoint_preserves_openai_input_image_remote_url(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)
    image_url = "https://example.com/diagram.png"

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "请分析这张图"},
                            {
                                "type": "input_image",
                                "image_url": image_url,
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["has_current_files"] is True
    assert runner.invocations[-1]["current_attachments"][0]["file_uri"] == image_url
    assert runner.invocations[-1]["current_attachments"][0]["mime_type"] == "image/*"
    assert runner.invocations[-1]["current_attachments"][0]["storage_path"] is None


@pytest.mark.asyncio
async def test_responses_endpoint_maps_openai_input_file_data_to_current_attachments(monkeypatch):
    _, runner, _, transport = _build_transport(monkeypatch)
    file_text = "候选人简历内容"
    file_b64 = base64.b64encode(file_text.encode("utf-8")).decode("ascii")

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
                                "filename": "resume.txt",
                                "file_data": file_b64,
                            },
                        ],
                    }
                ],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.invocations[-1]["has_current_files"] is True
    assert runner.invocations[-1]["current_attachments"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "data": file_b64,
            "is_text": True,
            "size_bytes": len(file_text.encode("utf-8")),
        }
    ]
    assert runner.invocations[-1]["attachment_results"][0]["text"] == file_text


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
    opened = {}

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
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.setattr(cmd_web_module.webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert fake_runner.load_agent_calls == 0
    assert opened["url"] == "http://localhost:8899"


def test_cmd_web_can_skip_browser_open(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()
    opened = {}

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
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.setattr(cmd_web_module.webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899", "--no-open"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert opened == {}


def test_cmd_web_reexecs_with_project_venv_python(monkeypatch, tmp_path):
    runner = CliRunner()
    project_dir = tmp_path / "demo-agent"
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

    import ksadk.cli.cmd_web as cmd_web_module

    captured: dict[str, object] = {}

    def _fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured["file"] = file
        captured["args"] = args
        captured["env"] = env
        raise SystemExit(23)

    import ksadk.cli.local_runtime as local_runtime

    monkeypatch.delenv("AGENTENGINE_WEB_VENV_REEXEC", raising=False)
    monkeypatch.delenv("AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC", raising=False)
    monkeypatch.setattr(local_runtime.sys, "executable", sys.executable, raising=False)
    monkeypatch.setattr(local_runtime.os, "execvpe", _fake_execvpe, raising=False)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 23
    assert captured["file"] == str(venv_python)
    args = captured["args"]
    assert isinstance(args, list)
    assert args[:2] == [str(venv_python), "-c"]
    assert "from ksadk.cli import main; main()" in args[2]
    assert args[3:] == [
        "web",
        str(project_dir.resolve()),
        "--port",
        "8899",
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["AGENTENGINE_LOCAL_RUNTIME_VENV_REEXEC"] == "1"
    assert str(Path(local_runtime.__file__).resolve().parents[2]) in args[2]


def test_cmd_web_does_not_reexec_inside_project_venv(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-agent"
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

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
    import ksadk.cli.local_runtime as local_runtime

    monkeypatch.setattr(local_runtime.sys, "executable", str(venv_python), raising=False)
    monkeypatch.setattr(
        local_runtime.os,
        "execvpe",
        lambda *_args, **_kwargs: pytest.fail("should not re-exec inside project venv"),
        raising=False,
    )
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]


@pytest.mark.parametrize("framework", ["adk", "langgraph", "langchain", "deepagents"])
def test_cmd_web_defaults_supported_framework_stm_to_persistent_sqlite(
    monkeypatch, tmp_path, framework
):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / f"demo-{framework}-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value=framework),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_STM_PATH", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_PATH", raising=False)
    monkeypatch.delenv("KSADK_STM_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.delenv("KSADK_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.cli.cmd_web.create_runner",
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


def test_cmd_web_preserves_explicit_stm_configuration(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-langgraph-agent"
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

    monkeypatch.setenv("KSADK_STM_BACKEND", "local")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/custom-sessions.db")
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.delenv("KSADK_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert os.environ["KSADK_STM_BACKEND"] == "local"
    assert os.environ["KSADK_STM_PATH"] == "/tmp/custom-sessions.db"


def test_cmd_web_preserves_partial_explicit_stm_configuration(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-langchain-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value="langchain"),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_STM_DB_PATH", "/tmp/legacy-custom-sessions.db")
    monkeypatch.delenv("KSADK_STM_PATH", raising=False)
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.delenv("KSADK_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert "KSADK_STM_BACKEND" not in os.environ
    assert "KSADK_STM_PATH" not in os.environ
    assert os.environ["KSADK_STM_DB_PATH"] == "/tmp/legacy-custom-sessions.db"


def test_cmd_web_exits_quietly_on_keyboard_interrupt(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _KeyboardInterruptServerRunner()
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
        "ksadk.cli.cmd_web.create_runner",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert "Traceback" not in result.output
    assert "统一 Web UI 启动失败" not in result.output


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
    assert "/agentengine/api/v1" in js_response.text
    for action_name in (
        "AttachmentContent",
        "UploadFile",
        "ListSessionEvents",
        "ListAgentModels",
        "RunAgent",
        "ListWorkspaceFiles",
        "AddWorkspaceFile",
        "DeleteWorkspaceFile",
        "GetWorkspaceFileContent",
    ):
        assert action_name in js_response.text
    assert "/run_sse" not in js_response.text
    assert "/agentengine/api/v1/models" not in js_response.text
    assert "overflow" in css_response.text


def _read_web_ui_source_or_skip(path: str) -> str:
    source_path = Path(path)
    if not source_path.exists():
        pytest.skip("ksadk-web is the canonical UI source; embedded web-ui source is optional")
    return source_path.read_text(encoding="utf-8")


def test_web_ui_source_uses_title_and_summary_in_sidebar():
    sidebar_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ChatSidebar.tsx"
    )
    session_helpers_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/utils/session-helpers.ts"
    )
    session_list_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/utils/session-list.js"
    )
    assert "session.Title" in session_helpers_source
    assert "session?.Summary" in session_list_source
    assert "session.SessionId.slice(0, 12)" not in sidebar_source


def test_web_ui_source_supports_clipboard_file_paste():
    composer_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ConnectedComposer.tsx"
    )
    attachment_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/utils/attachment.ts")
    assert "clipboardData.items" in attachment_source
    assert "onPaste" in composer_source
    assert "getAsFile" in attachment_source


def test_web_ui_source_prefers_responses_when_runtime_supports_it():
    bootstrap_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/hooks/useBootstrap.ts")
    layout_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/utils/layout-constants.ts")
    run_engine_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/core/run/engine.ts")
    assert "setAgentFramework" in bootstrap_source
    assert "if (apiFormats.includes('responses'))" in layout_source
    assert "return 'responses'" in layout_source
    assert "resolveRunAgentApiFormat({ agentFramework: this.config.agentFramework, apiFormats: this.config.apiFormats })" in run_engine_source


def test_static_workbench_uses_openai_responses_content_for_inline_attachments():
    index_html = Path("ksadk/server/static/index.html").read_text(encoding="utf-8")
    match = re.search(r'src="\.\/(assets\/index-[^"]+\.js)"', index_html)
    assert match, "static index.html should reference the built Vite entry bundle"
    source = Path("ksadk/server/static", match.group(1)).read_text(encoding="utf-8")

    assert "type:`input_image`" in source
    assert "image_url:await this.imageFileToDataUrl" in source
    assert "type:`input_file`" in source
    assert "filename:" in source
    assert "file_url:" in source
    assert "inlineData: {" not in source


def test_web_ui_run_engine_uses_responses_input_for_responses_protocol():
    run_engine_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/core/run/engine.ts")

    assert "body.ResponsesInput = [{ role: 'user', content: parts }]" in run_engine_source
    assert "body.Messages = [{ role: 'user', content: parts }]" in run_engine_source


def test_web_ui_source_supports_workspace_panel_for_owner_access():
    source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/App.tsx")
    header_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ChatHeader.tsx"
    )
    api_facade_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/core/api/facade.ts")
    bootstrap_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/hooks/useBootstrap.ts")
    layout_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/utils/layout-constants.ts")
    run_engine_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/core/run/engine.ts")
    workspace_api_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/api/workspace.ts")
    workspace_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/workspace/WorkspacePanel.tsx"
    )
    workspace_utils_source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/utils/workspace.js")
    session_events_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/utils/session-events.js"
    )
    responses_stream_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/utils/responses-stream.js"
    )
    assert "WorkspaceFiles" in source
    assert "canAccessWorkspaceFiles({ workspaceFiles, accessMode })" in source
    assert "mode === 'owner' || mode === 'private'" in workspace_utils_source
    assert "WorkspacePanel" in source
    assert "flex h-14 flex-shrink-0 items-center" in workspace_source
    assert "listWorkspaceFiles" in api_facade_source
    assert "addWorkspaceFile" in api_facade_source
    assert "capability.ContentPath" in workspace_source
    assert "DeleteWorkspaceFile" in workspace_api_source
    assert "workspaceEnabled" in header_source
    assert "ApiFormat: apiFormat" in run_engine_source
    assert "Model: this.config.selectedModel || undefined" in run_engine_source
    assert "chat_completions" in bootstrap_source
    assert "chat_completions" in layout_source
    assert "normalizeResponsesStreamEvent" in session_events_source
    assert "extractCompletedText" in responses_stream_source
    assert "item.delta" in responses_stream_source
    assert "response.output_item.added" in responses_stream_source
    assert "response.function_call_arguments.delta" in responses_stream_source
    assert "response.ksadk.tool_result" in responses_stream_source
    assert "response.ksadk.approval_request" in responses_stream_source


def test_web_ui_source_supports_streaming_queue_and_refresh_pending_status():
    source = _read_web_ui_source_or_skip("ksadk/server/web-ui/src/App.tsx")
    connected_composer_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ConnectedComposer.tsx"
    )
    composer_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ChatComposer.tsx"
    )
    sidebar_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ChatSidebar.tsx"
    )
    session_events_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/utils/session-events.js"
    )
    assert "queuedDraftRef" in source
    assert "queuedDrafts={queuedDrafts}" in connected_composer_source
    assert "latestRunStatusByInvocation" in session_events_source
    assert "event.EventType !== 'run_status'" in session_events_source
    assert "meta.running" in sidebar_source
    assert "disabled={!isStreaming && !input.trim() && attachments.length === 0}" in composer_source
    assert "发送队列 · {queuedDrafts.length}" in composer_source
    assert "当前回复完成后依次发送" in composer_source
    assert "title={isStreaming ? '停止生成' : '发送消息'}" in composer_source
    assert "flex flex-shrink-0 flex-col gap-2" in sidebar_source


def test_web_ui_source_threads_generation_controls_into_message_list():
    source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ChatMessageList.tsx"
    )
    assert "onStopGeneration," in source
    assert "onCancelRemote," in source


def test_web_ui_source_uses_adaptive_image_preview_sizing():
    connected_message_list_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/ConnectedMessageList.tsx"
    )
    preview_source = _read_web_ui_source_or_skip(
        "ksadk/server/web-ui/src/components/chat/AttachmentPreview.tsx"
    )
    assert "naturalWidth" in preview_source
    assert "naturalHeight" in preview_source
    assert "setPreviewImageSize" in connected_message_list_source
