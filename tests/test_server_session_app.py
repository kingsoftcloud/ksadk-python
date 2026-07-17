from __future__ import annotations

import base64
import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.background import BackgroundTask
from fastapi.responses import Response

import ksadk.conversations as conversation
from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest, InlineData, Part
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.errors import SessionBackendUnavailable
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
        self.calls.append(input_data)
        yield {"type": "final", "output": "assistant says hi"}


class _CustomUiRunner(_DummyRunner):
    def __init__(self, project_dir: str):
        super().__init__()
        self.project_dir = project_dir
        self.detection_result.type = SimpleNamespace(value="langgraph")


class _CheckpointResumeRunner(_DummyRunner):
    def describe_checkpoint_capability(self) -> dict:
        return {
            "Supported": True,
            "Backend": "postgres",
            "Scope": "shared",
            "Durable": True,
            "SharedAcrossPods": True,
            "Reason": "",
        }

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {
            "output": "resumed from checkpoint",
            "metadata": {
                "agentengine": {
                    "run_id": str(input_data.get("run_id") or ""),
                    "framework": "langgraph",
                    "framework_ref": input_data.get("framework_ref") or {},
                }
            },
        }


class _CheckpointMetadataRunner(_DummyRunner):
    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        session_id = str(input_data.get("session_id") or "")
        return {
            "output": "checkpoint ready",
            "metadata": {
                "agentengine": {
                    "run_id": "run-hosted",
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "thread_id": session_id,
                            "checkpoint_id": "ckpt-hosted",
                        }
                    },
                }
            },
        }


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


class _ThinkingOnlyFinalRunner(_OverrideStreamingRunner):
    async def stream(self, input_data: dict):
        yield {"type": "thinking", "delta": "先想一下"}
        yield {"type": "final", "output": "final answer"}


class _SlowStreamingRunner(_OverrideStreamingRunner):
    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "hel"}
        await asyncio.sleep(0.05)
        yield {"type": "text", "delta": "lo"}
        yield {"type": "final", "output": "hello"}


class _UnavailableSessionService(InMemorySessionService):
    async def create_session(self, *args, **kwargs):
        raise SessionBackendUnavailable("Postgres session backend unavailable")

    async def list_sessions(self, *args, **kwargs):
        raise SessionBackendUnavailable("Postgres session backend unavailable")

    async def count_sessions(self, *args, **kwargs):
        raise SessionBackendUnavailable("Postgres session backend unavailable")


class _CancellableStreamingRunner(_OverrideStreamingRunner):
    def __init__(self):
        super().__init__()
        self.cancel_requests: list[str] = []

    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "hel"}
        await asyncio.Event().wait()

    def request_cancel(self, invocation_id: str) -> str:
        self.cancel_requests.append(invocation_id)
        return "accepted"


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
async def test_ui_bootstrap_disables_checkpoint_controls_when_runner_does_not_support_them(
    monkeypatch,
):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    run_lifecycle = response.json()["Data"]["Capabilities"]["RunLifecycle"]
    assert run_lifecycle["Enabled"] is True
    assert run_lifecycle["Resume"] is True
    assert run_lifecycle["Abort"] is True
    assert run_lifecycle["Checkpoints"] is False
    assert run_lifecycle["CheckpointResume"] is False
    assert run_lifecycle["CheckpointResumePreview"] is False
    capabilities = response.json()["Data"]["Capabilities"]
    assert capabilities["RuntimeCapabilities"]["Framework"] == "mock"
    assert capabilities["RuntimeCapabilities"]["Checkpoint"]["Supported"] is False
    assert capabilities["RuntimeCapabilities"]["ResumeRun"]["ResumeMode"] == "none"
    assert capabilities["CheckpointResumeCapability"]["Supported"] is False


@pytest.mark.asyncio
async def test_ui_bootstrap_enables_checkpoint_controls_from_runtime_capability(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    capabilities = response.json()["Data"]["Capabilities"]
    assert capabilities["CheckpointResumeCapability"]["Supported"] is True
    assert capabilities["RunLifecycle"]["Checkpoints"] is True
    assert capabilities["RunLifecycle"]["CheckpointResume"] is True
    assert capabilities["RunLifecycle"]["CheckpointResumePreview"] is True


@pytest.mark.asyncio
async def test_ui_bootstrap_exposes_custom_ui_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    project_dir = "/tmp/custom-ui-agent"
    runner = _CustomUiRunner(project_dir)

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    monkeypatch.setattr(
        server_app_module,
        "_resolve_agent_ui_spec",
        lambda: {
            "enabled": True,
            "ui_profile": "custom",
            "ui_path": "/",
            "ui_url": None,
            "ui_bundle_path": f"{project_dir}/research-ui/dist",
        },
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Data"]["SharePermissions"] == {
        "Interactive": True,
        "DefaultPath": "/",
        "SharePath": "/",
    }
    assert payload["Data"]["CustomUI"] == {
        "Enabled": True,
        "Profile": "custom",
        "Path": "/",
        "Url": None,
        "BundlePath": f"{project_dir}/research-ui/dist",
    }


@pytest.mark.asyncio
async def test_ui_bootstrap_defaults_to_runtime_agent_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-hosted-runtime")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post("/agentengine/api/v1/GetAgentUiBootstrap", json={})

    assert response.status_code == 200
    assert response.json()["Data"]["Agent"]["AgentId"] == "ar-hosted-runtime"


def test_resolve_agent_ui_spec_uses_custom_ui_env_fallback(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    project_dir = tmp_path / "agent"
    bundle_dir = project_dir / "research-ui" / "dist"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "index.html").write_text("<title>Custom UI</title>", encoding="utf-8")
    runner = _CustomUiRunner(str(project_dir))

    server_app_module.set_runner(runner)
    monkeypatch.setenv("KSADK_UI_PROFILE", "custom")
    monkeypatch.setenv("KSADK_UI_PATH", "/")
    monkeypatch.setenv("KSADK_UI_BUNDLE_PATH", "research-ui/dist")

    spec = server_app_module._resolve_agent_ui_spec()

    assert spec["enabled"] is True
    assert spec["ui_profile"] == "custom"
    assert spec["ui_path"] == "/"
    assert spec["source"] == "custom"
    assert spec["ui_bundle_path"] == str(bundle_dir.resolve())


def test_resolve_agent_ui_spec_auto_detects_project_custom_ui_bundle(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    project_dir = tmp_path / "agent"
    bundle_dir = project_dir / "research-ui" / "dist"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "index.html").write_text("<title>Custom UI</title>", encoding="utf-8")
    runner = _CustomUiRunner(str(project_dir))

    server_app_module.set_runner(runner)
    monkeypatch.delenv("KSADK_UI_PROFILE", raising=False)
    monkeypatch.delenv("KSADK_UI_PATH", raising=False)
    monkeypatch.delenv("KSADK_UI_BUNDLE_PATH", raising=False)

    spec = server_app_module._resolve_agent_ui_spec()

    assert spec["enabled"] is True
    assert spec["ui_profile"] == "custom"
    assert spec["ui_path"] == "/"
    assert spec["source"] == "custom"


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
    # run_status 事件现在携带 state_delta.active_run（与 agentengine-server 对齐），
    # completed 后 active_run 反映终态。
    assert session.state["topic"] == "billing"
    assert session.state["active_run"]["status"] == "completed"
    # active_run 现含 run_mode/run_trigger（普通前台 run 默认 foreground/new_run）
    assert session.state["active_run"]["run_mode"] == "foreground"
    assert session.state["active_run"]["run_trigger"] == "new_run"

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
            "input_content": [{"type": "input_text", "text": "hello"}],
            "input_messages": [
                {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
            ],
            "input_parts": [{"text": "hello"}],
            "attachments": [],
            "attachment_results": [],
            "current_attachments": [],
            "current_attachment_results": [],
            "has_current_files": False,
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
    assert runner.calls[-1]["current_attachments"] == [
        {
            "display_name": "resume.txt",
            "mime_type": "text/plain",
            "transport": "inline",
            "data": base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii"),
            "is_text": True,
            "size_bytes": len("候选人简历内容".encode("utf-8")),
        }
    ]
    assert runner.calls[-1]["current_attachment_results"] == [
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
    assert runner.calls[-1]["has_current_files"] is True
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
async def test_workspace_files_runtime_routes_use_state_dir_workspace_root(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "existing").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "existing" / "hello.txt").write_text("hello workspace", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        list_response = await client.get("/_ksadk/workspace/v1/entries", params={"path": "."})
        upload_response = await client.post(
            "/_ksadk/workspace/v1/files/uploads/report.txt",
            files={"file": ("report.txt", b"workspace upload", "text/plain")},
        )
        download_response = await client.get("/_ksadk/workspace/v1/files/uploads/report.txt")

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["Root"] == "workspace"
    assert list_payload["Path"] == "."
    assert {entry["Path"] for entry in list_payload["Entries"]} == {"existing"}
    assert list_payload["Entries"][0]["Type"] == "directory"

    assert upload_response.status_code == 200
    assert upload_response.json()["Entry"]["Path"] == "uploads/report.txt"
    assert (workspace_dir / "uploads" / "report.txt").read_text(encoding="utf-8") == "workspace upload"

    assert download_response.status_code == 200
    assert download_response.content == b"workspace upload"
    assert download_response.headers["content-type"].startswith("text/plain")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        delete_response = await client.delete("/_ksadk/workspace/v1/files/uploads/report.txt")

    assert delete_response.status_code == 200
    assert delete_response.json() == {"Deleted": True}
    assert not (workspace_dir / "uploads" / "report.txt").exists()


@pytest.mark.asyncio
async def test_workspace_files_runtime_routes_delete_empty_directory(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    empty_dir = workspace_dir / "empty-folder"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        runtime_response = await client.delete("/_ksadk/workspace/v1/files/empty-folder")

    assert runtime_response.status_code == 200
    assert runtime_response.json() == {"Deleted": True}
    assert not empty_dir.exists()


@pytest.mark.asyncio
async def test_workspace_files_runtime_routes_delete_empty_directory_with_trailing_slash(
    monkeypatch,
    tmp_path,
):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    empty_dir = workspace_dir / "empty-folder"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        runtime_response = await client.delete("/_ksadk/workspace/v1/files/empty-folder/")

    assert runtime_response.status_code == 200
    assert runtime_response.json() == {"Deleted": True}
    assert not empty_dir.exists()


@pytest.mark.asyncio
async def test_workspace_files_action_route_deletes_empty_directory(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    empty_dir = workspace_dir / "empty-folder"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        action_response = await client.post(
            "/agentengine/api/v1/DeleteWorkspaceFile",
            json={"AgentId": "demo-agent", "Path": "empty-folder"},
        )

    assert action_response.status_code == 200
    assert action_response.json()["Data"] == {"Deleted": True}
    assert not empty_dir.exists()


@pytest.mark.asyncio
async def test_workspace_files_runtime_routes_reject_non_empty_directory_delete(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    non_empty_dir = workspace_dir / "docs"
    non_empty_dir.mkdir(parents=True, exist_ok=True)
    (non_empty_dir / "readme.txt").write_text("keep me", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.delete("/_ksadk/workspace/v1/files/docs")

    assert response.status_code == 409
    assert response.json()["detail"] == "workspace directory is not empty"
    assert (non_empty_dir / "readme.txt").exists()


@pytest.mark.asyncio
async def test_workspace_files_runtime_route_serves_html_preview_inline(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "showcase").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "showcase" / "index.html").write_text(
        '<html><head></head><body><a href="#features">Features</a></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get("/_ksadk/workspace/v1/files/showcase/index.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "content-disposition" not in response.headers
    csp = response.headers.get("content-security-policy", "")
    assert "sandbox allow-scripts allow-downloads" in csp
    assert "style-src 'unsafe-inline' data: 'self' https:" in csp
    assert "img-src data: blob: 'self' https:" in csp
    assert "connect-src 'none'" in csp
    assert '<base href="/_ksadk/workspace/v1/files/showcase/">' in response.text
    assert "data-ksadk-preview-anchor-handler" in response.text


@pytest.mark.asyncio
async def test_workspace_files_runtime_routes_reject_path_escape(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get(
            "/_ksadk/workspace/v1/entries",
            params={"path": "../outside"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "workspace path escapes the workspace root"


@pytest.mark.asyncio
async def test_workspace_files_action_routes_match_runtime_contract(monkeypatch, tmp_path):
    server_app_module = importlib.import_module("ksadk.server.app")
    ui_dir = tmp_path / ".agentengine" / "ui"
    workspace_dir = ui_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "existing").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "existing" / "hello.txt").write_text("hello workspace", encoding="utf-8")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(ui_dir))

    service = InMemorySessionService()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        list_response = await client.post(
            "/agentengine/api/v1/ListWorkspaceFiles",
            json={"AgentId": "demo-agent", "Path": "."},
        )
        upload_response = await client.post(
            "/agentengine/api/v1/AddWorkspaceFile",
            data={"AgentId": "demo-agent", "Path": "uploads/report.txt"},
            files={"file": ("report.txt", b"workspace upload", "text/plain")},
        )
        download_response = await client.get(
            "/agentengine/api/v1/GetWorkspaceFileContent",
            params={"AgentId": "demo-agent", "FilePath": "uploads/report.txt"},
        )

    assert list_response.status_code == 200
    list_payload = list_response.json()["Data"]
    assert list_payload["Root"] == "workspace"
    assert list_payload["Path"] == "."
    assert {entry["Path"] for entry in list_payload["Entries"]} == {"existing"}

    assert upload_response.status_code == 200
    assert upload_response.json()["Data"]["Entry"]["Path"] == "uploads/report.txt"
    assert (workspace_dir / "uploads" / "report.txt").read_text(encoding="utf-8") == "workspace upload"

    assert download_response.status_code == 200
    assert download_response.content == b"workspace upload"
    assert download_response.headers["content-type"].startswith("text/plain")

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        delete_response = await client.post(
            "/agentengine/api/v1/DeleteWorkspaceFile",
            json={"AgentId": "demo-agent", "Path": "uploads/report.txt"},
        )

    assert delete_response.status_code == 200
    assert delete_response.json()["Data"] == {"Deleted": True}
    assert not (workspace_dir / "uploads" / "report.txt").exists()


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
async def test_list_sessions_prefers_active_resume_invocation_over_old_terminal_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-active-resume",
    )
    await service.append_event(
        "sess-active-resume",
        SessionEvent(
            author="demo",
            event_type="run_status",
            invocation_id="run-original",
            content={"status": "completed"},
            metadata={"status": "completed", "run_id": "run-original"},
        ),
    )
    await service.append_event(
        "sess-active-resume",
        SessionEvent(
            author="demo",
            event_type="run_resume",
            invocation_id="run-resume-1",
            content={"checkpoint_id": "ckpt-1"},
            metadata={
                "run_id": "run-original",
                "checkpoint_id": "ckpt-1",
                "resume_attempt_id": "run-resume-1",
            },
        ),
    )
    await service.append_event(
        "sess-active-resume",
        SessionEvent(
            author="demo",
            event_type="run_status",
            invocation_id="run-resume-1",
            content={"status": "in_progress"},
            metadata={"status": "in_progress", "run_id": "run-resume-1"},
        ),
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
    assert session["ActiveInvocationId"] == "run-resume-1"
    assert session["ActiveRunStatus"] == "in_progress"


@pytest.mark.asyncio
async def test_runtime_local_list_sessions_returns_page_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    for index in range(5):
        await service.create_session(
            agent_id="demo-agent",
            user_id="user-1",
            session_id=f"sess-page-{index}",
        )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={
                "AgentId": "demo-agent",
                "UserId": "user-1",
                "Page": 2,
                "PageSize": 2,
            },
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Page"] == 2
    assert data["PageSize"] == 2
    assert data["Total"] == 5
    assert len(data["Sessions"]) == 2


@pytest.mark.asyncio
async def test_list_sessions_hydrates_summary_from_event_log_when_session_row_is_empty(monkeypatch):
    """ListSessions 不能只返回空壳 session；event log 已有事实时要回填标题与 active run。"""
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-event-backed-summary",
    )
    user_prompt = (
        "请启动 Deep Research 调研任务，不要只做普通聊天回答。\n\n"
        "研究主题：调研 2026 Q2和Q3金山云股票风险和评估建议\n\n"
        "研究深度：med。"
    )
    await conversation_runtime.append_conversation_event(
        session_id="sess-event-backed-summary",
        author="user",
        role="user",
        text=user_prompt,
        invocation_id="run_sess_event_backed_summary",
        event_type="user_message",
        metadata={"agent_input": user_prompt},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_status_event(
        session_id="sess-event-backed-summary",
        author="demo-agent",
        status="in_progress",
        invocation_id="run_sess_event_backed_summary",
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_conversation_event(
        session_id="sess-event-backed-summary",
        author="demo-agent",
        role="model",
        text="Planner 已生成 5 个检索 query。",
        invocation_id="run_sess_event_backed_summary",
        event_type="stage_tool_result",
        metadata={
            "run_id": "run_sess_event_backed_summary",
            "tool_output": {
                "topic": "调研 2026 Q2和Q3金山云股票风险和评估建议",
            },
        },
        session_service_provider=lambda: service,
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
    normalized_prompt = " ".join(user_prompt.split())
    assert session["SessionId"] == "sess-event-backed-summary"
    assert session["FirstPrompt"] == normalized_prompt
    assert session["LastPrompt"] == normalized_prompt
    assert session["Title"]
    assert session["Title"] != "sess-event-backed-summary"
    assert session["ActiveInvocationId"] == "run_sess_event_backed_summary"
    assert session["ActiveRunStatus"] == "in_progress"


@pytest.mark.asyncio
async def test_session_actions_prefer_latest_run_status_when_previous_run_completed(monkeypatch):
    """切换/刷新会话时，新后台 run 进行中不能被同 session 的旧 completed run 盖掉。"""
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-new-run-active",
    )

    await conversation_runtime.append_run_status_event(
        session_id=session.id,
        author="demo-agent",
        status="completed",
        invocation_id="run_old_completed",
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_status_event(
        session_id=session.id,
        author="demo-agent",
        status="in_progress",
        invocation_id="run_new_active",
        session_service_provider=lambda: service,
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        listed = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        fetched = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": session.id},
        )

    assert listed.status_code == 200
    assert fetched.status_code == 200
    for payload in (
        listed.json()["Data"]["Sessions"][0],
        fetched.json()["Data"]["Session"],
    ):
        assert payload["ActiveInvocationId"] == "run_new_active"
        assert payload["ActiveRunStatus"] == "in_progress"


@pytest.mark.asyncio
async def test_session_actions_return_503_when_session_backend_unavailable(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = _UnavailableSessionService()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        create_response = await client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        list_response = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )

    assert create_response.status_code == 503
    assert create_response.json()["detail"]["code"] == "session_backend_unavailable"
    assert list_response.status_code == 503
    assert list_response.json()["detail"]["code"] == "session_backend_unavailable"


@pytest.mark.asyncio
async def test_session_actions_do_not_return_inline_attachment_data_in_state(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-inline-state",
    )
    await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id=session.id,
        scope="session",
        state_delta={
            "__ksadk_attachment_context__": {
                "attachments": [
                    {
                        "display_name": "photo.png",
                        "mime_type": "image/png",
                        "transport": "inline",
                        "data": base64.b64encode(b"image bytes").decode("ascii"),
                        "size_bytes": 11,
                    }
                ],
                "attachment_results": [
                    {
                        "display_name": "photo.png",
                        "mime_type": "image/png",
                        "transport": "inline",
                        "text": "识别出的文字",
                        "text_excerpt": "识别出的文字",
                    }
                ],
            }
        },
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        listed = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        fetched = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": session.id},
        )

    assert listed.status_code == 200
    assert fetched.status_code == 200
    for payload in (
        listed.json()["Data"]["Sessions"][0],
        fetched.json()["Data"]["Session"],
    ):
        state_context = payload["State"]["__ksadk_attachment_context__"]
        attachment = state_context["attachments"][0]
        assert attachment == {
            "display_name": "photo.png",
            "mime_type": "image/png",
            "transport": "inline",
            "size_bytes": 11,
        }
        assert "data" not in json.dumps(state_context, ensure_ascii=False)
        assert state_context["attachment_results"][0]["text_excerpt"] == "识别出的文字"
        assert "text" not in state_context["attachment_results"][0]


@pytest.mark.asyncio
async def test_local_feedback_actions_upsert_get_and_delete(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-local-feedback",
    )
    assistant_event = await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "event_type": "assistant_message",
                "content": {"role": "model", "parts": [{"text": "assistant says hi"}]},
                "metadata": {
                    "response_id": "resp_local_feedback",
                    "trace_id": "trace-local",
                    "root_span_id": "span-local",
                },
            },
            session_id=session.id,
        ),
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/agentengine/api/v1/UpsertResponseFeedback",
            json={
                "AgentId": "demo-agent",
                "SessionId": session.id,
                "ResponseId": "resp_local_feedback",
                "EventId": assistant_event.id,
                "Rating": "down",
                "Comment": "不够具体",
            },
        )
        fetched = await client.post(
            "/agentengine/api/v1/GetResponseFeedback",
            json={
                "AgentId": "demo-agent",
                "SessionId": session.id,
                "ResponseId": "resp_local_feedback",
            },
        )
        deleted = await client.post(
            "/agentengine/api/v1/DeleteResponseFeedback",
            json={
                "AgentId": "demo-agent",
                "SessionId": session.id,
                "ResponseId": "resp_local_feedback",
            },
        )
        fetched_after_delete = await client.post(
            "/agentengine/api/v1/GetResponseFeedback",
            json={
                "AgentId": "demo-agent",
                "SessionId": session.id,
                "ResponseId": "resp_local_feedback",
            },
        )

    assert created.status_code == 200
    feedback = created.json()["Data"]["Feedback"]
    assert feedback["AgentId"] == "demo-agent"
    assert feedback["SessionId"] == session.id
    assert feedback["ResponseId"] == "resp_local_feedback"
    assert feedback["EventId"] == assistant_event.id
    assert feedback["Rating"] == "down"
    assert feedback["Comment"] == "不够具体"
    assert feedback["TraceId"] == "trace-local"
    assert feedback["RootSpanId"] == "span-local"

    assert fetched.status_code == 200
    assert fetched.json()["Data"]["Feedback"]["Rating"] == "down"
    assert deleted.status_code == 200
    assert deleted.json()["Data"] == {"Deleted": True}
    assert fetched_after_delete.status_code == 200
    assert fetched_after_delete.json()["Data"]["Feedback"] is None


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
async def test_run_sse_stream_completes_and_persists_reasoning_when_no_text_deltas(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _ThinkingOnlyFinalRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId="sess-run-sse-thinking",
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=True,
            ).model_dump(),
        )

    assert response.status_code == 200
    events = await service.get_events("sess-run-sse-thinking")
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "reasoning",
        "assistant_message",
        "run_status",
    ]
    assert events[2].content["parts"][0]["text"] == "先想一下"
    assert events[-2].content["parts"][0]["text"] == "final answer"
    assert events[-1].content["status"] == "completed"


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
                "model": "glm-5.1",
                "account_id": "acct-chat",
            },
        )

    assert response.status_code == 200
    assert runner.prepared_models == ["glm-5.1"]
    assert runner.calls[-1]["model"] == "glm-5.1"
    assert runner.calls[-1]["platform_context"]["account_id"] == "acct-chat"


@pytest.mark.asyncio
async def test_chat_completions_converts_chat_content_blocks_to_runner_responses_input(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    image_url = "data:image/png;base64,aW1hZ2U="
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看图"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                "stream": False,
                "model": "gpt-4o",
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert runner.calls[-1]["input_content"] == [
        {"type": "input_text", "text": "看图"},
        {"type": "input_image", "image_url": image_url},
    ]
    assert runner.calls[-1]["input_messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": image_url},
            ],
        }
    ]
    assert runner.calls[-1]["input_parts"] == [
        {"text": "看图"},
        {
            "inlineData": {
                "data": "aW1hZ2U=",
                "mimeType": "image/png",
                "displayName": "uploaded_image",
            }
        },
    ]


@pytest.mark.asyncio
async def test_chat_completions_non_stream_preserves_response_feedback_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    runner = _ModelAwareRunner()

    async def _fake_invoke_conversation_once(**kwargs):
        return "sess-trace", {
            "output_text": "assistant says hi",
            "metadata": {
                "trace_id": "08c19ddddce0b1ddd29407dc637e1c89",
                "root_span_id": "74cc406c8e9ded4a",
            },
        }

    monkeypatch.setattr(
        server_app_module.conversation,
        "invoke_conversation_once",
        _fake_invoke_conversation_once,
    )
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
                "model": "glm-5.1",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"] == {
        "trace_id": "08c19ddddce0b1ddd29407dc637e1c89",
        "root_span_id": "74cc406c8e9ded4a",
    }


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
            "size_bytes": len("候选人简历内容".encode("utf-8")),
            "kind": "text",
            "status": "ok",
            "warnings": [],
            "extraction_method": "text_decode",
            "text_excerpt": "候选人简历内容",
        }
    ]


@pytest.mark.asyncio
async def test_list_agent_models_action_normalizes_default_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    real_async_client = httpx.AsyncClient
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={"data": [{"id": "glm-5.1"}]},
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
    assert payload["Current"] == "glm-5.1"
    assert payload["Models"] == [
        {
            "id": "glm-5.1",
            "display_name": "glm-5.1",
            "context_window_tokens": 200000,
            "max_output_tokens": 32000,
            "auto_compact_threshold_tokens": 167000,
            "auto_compact_threshold_percentage": 84,
            "capabilities": {
                "function_calling": True,
                "structured_output": True,
                "context_caching": True,
                "multimodal_input_image": False,
                "multimodal_input_video": False,
                "multimodal_input_file": False,
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
    monkeypatch.setenv("OPENAI_MODEL_NAME", "kimi-k2.6")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={
                "data": [
                    {
                        "id": "kimi-k2.6",
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
    assert item["id"] == "kimi-k2.6"
    assert item["owned_by"] == "ksyun"
    assert item["context_length"] == 131072
    assert item["max_tokens"] == 4096
    assert item["context_window_tokens"] == 131072
    assert item["max_output_tokens"] == 4096
    assert item["limits"]["context_window_tokens"] == 131072
    assert item["limits"]["max_output_tokens"] == 4096


@pytest.mark.asyncio
async def test_list_agent_models_action_normalizes_kspmas_string_token_limits(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    real_async_client = httpx.AsyncClient
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={
                "data": [
                    {
                        "id": "glm-5.1",
                        "context_length": "200k",
                        "max_completion_tokens": "128k",
                        "architecture": {
                            "input_modalities": ["文字"],
                            "output_modalities": ["文字"],
                        },
                        "pricing": {
                            "prompt": "6",
                            "completion": "24",
                        },
                    },
                    {
                        "id": "deepseek-v3.2",
                        "context_length": "128",
                        "max_completion_tokens": "32",
                    },
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
    items = {item["id"]: item for item in response.json()["Data"]["Models"]}
    assert items["glm-5.1"]["context_window_tokens"] == 200000
    assert items["glm-5.1"]["max_output_tokens"] == 128000
    assert items["glm-5.1"]["limits"]["context_window_tokens"] == 200000
    assert items["glm-5.1"]["limits"]["max_output_tokens"] == 128000
    assert items["glm-5.1"]["auto_compact_threshold_tokens"] == 167000
    assert items["glm-5.1"]["architecture"]["input_modalities"] == ["文字"]
    assert items["glm-5.1"]["capabilities"]["multimodal_input_image"] is False
    assert items["glm-5.1"]["pricing"]["prompt"] == "6"
    assert items["deepseek-v3.2"]["context_window_tokens"] == 128000
    assert items["deepseek-v3.2"]["max_output_tokens"] == 32000
    assert items["deepseek-v3.2"]["auto_compact_threshold_tokens"] == 95000


@pytest.mark.asyncio
async def test_list_agent_models_action_without_api_base_returns_default_metadata(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListAgentModels",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["Current"] == "glm-5.1"
    assert [item["id"] for item in payload["Models"]] == ["glm-5.1"]
    assert payload["Models"][0]["context_window_tokens"] == 200000
    assert payload["Models"][0]["limits"]["max_output_tokens"] == 32000


@pytest.mark.asyncio
async def test_openai_models_route_exposes_current_catalog(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    transport = httpx.ASGITransport(app=server_app_module.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["current"] == "glm-5.1"
    assert [item["id"] for item in payload["data"]] == ["glm-5.1"]


@pytest.mark.asyncio
async def test_responses_fetches_remote_model_metadata_and_passes_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    real_async_client = httpx.AsyncClient
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "runner", runner)
    monkeypatch.setattr(server_app_module, "_runner_loaded", True)
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *args, **kwargs: _ExternalModelsAsyncClient(
            *args,
            payload={
                "data": [
                    {
                        "id": "kimi-k2.6",
                        "architecture": {
                            "input_modalities": ["文字", "图片", "视频"],
                            "output_modalities": ["文字"],
                        },
                    }
                ]
            },
            **kwargs,
        ),
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with real_async_client(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "model": "kimi-k2.6",
                "input": "请分析图片",
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert runner.calls[0]["model_metadata"]["id"] == "kimi-k2.6"
    assert runner.calls[0]["model_metadata"]["architecture"]["input_modalities"] == ["文字", "图片", "视频"]
    assert runner.calls[0]["model_metadata"]["capabilities"]["multimodal_input_image"] is True


@pytest.mark.asyncio
async def test_responses_uses_official_conversation_as_runtime_session(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-a",
                "safety_identifier": "user-a",
                "account_id": "acct-a",
                "stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "conv-a"
    session = await service.get_session("conv-a")
    assert session is not None
    assert session.user_id == "user-a"
    assert runner.calls[-1]["session_id"] == "conv-a"
    assert runner.calls[-1]["platform_context"]["user_id"] == "user-a"
    assert runner.calls[-1]["platform_context"]["account_id"] == "acct-a"


@pytest.mark.asyncio
async def test_responses_uses_runtime_agent_id_for_hosted_session_lifecycle(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CheckpointMetadataRunner()

    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-hosted-runtime")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-hosted",
                "stream": False,
            },
        )
        checkpoints_response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "ar-hosted-runtime", "SessionId": "conv-hosted"},
        )

    assert response.status_code == 200
    session = await service.get_session("conv-hosted")
    assert session is not None
    assert session.agent_id == "ar-hosted-runtime"
    assert checkpoints_response.status_code == 200
    checkpoints = checkpoints_response.json()["Data"]["Checkpoints"]
    assert checkpoints[0]["RunId"] == "run-hosted"
    assert checkpoints[0]["CheckpointId"] == "ckpt-hosted"


@pytest.mark.asyncio
async def test_responses_accepts_agentengine_checkpoint_resume_input(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-a", session_id="conv-resume")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="conv-resume",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:conv-resume",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "type": "agentengine.resume_checkpoint",
                        "run_id": "run-1",
                        "checkpoint_id": "ckpt-1",
                        "resume_attempt_id": "resume-1",
                        "framework": "langgraph",
                        "framework_ref": {
                            "langgraph": {
                                "thread_id": "forged-client-thread",
                                "checkpoint_id": "forged-client-checkpoint",
                            }
                        },
                    }
                ],
                "conversation": "conv-resume",
                "safety_identifier": "user-a",
                "stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["agentengine"]["run_id"] == "run-1"
    assert payload["metadata"]["agentengine"]["framework_ref"]["langgraph"]["checkpoint_id"] == "ckpt-1"
    assert runner.calls[-1]["checkpoint_resume"] is True
    assert runner.calls[-1]["run_id"] == "run-1"
    assert runner.calls[-1]["framework_ref"]["langgraph"]["thread_id"] == "tenant:agent:conv-resume"
    assert runner.calls[-1]["framework_ref"]["langgraph"]["checkpoint_id"] == "ckpt-1"
    events = await service.get_events("conv-resume")
    assert [event.event_type for event in events] == [
        "run_checkpoint",
        "run_resume",
        "run_status",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    # resume 现在会先写 run_status(resuming) 再写 run_status(in_progress)
    assert [event.content["status"] for event in events if event.event_type == "run_status"] == [
        "resuming",
        "in_progress",
        "completed",
    ]
    assert len([event for event in events if event.event_type == "run_checkpoint"]) == 1


@pytest.mark.asyncio
async def test_responses_rejects_agentengine_checkpoint_resume_without_server_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-a", session_id="conv-resume-missing")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [
                    {
                        "type": "agentengine.resume_checkpoint",
                        "run_id": "run-1",
                        "checkpoint_id": "ckpt-1",
                        "framework": "langgraph",
                        "framework_ref": {
                            "langgraph": {
                                "thread_id": "client-only-thread",
                                "checkpoint_id": "ckpt-1",
                            }
                        },
                    }
                ],
                "conversation": "conv-resume-missing",
                "safety_identifier": "user-a",
                "stream": False,
            },
        )

    assert response.status_code == 404
    assert runner.calls == []
    assert await service.get_events("conv-resume-missing") == []


@pytest.mark.asyncio
async def test_stream_responses_checkpoint_resume_rejects_concurrent_resume_for_same_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-a", session_id="conv-resume-stream")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="conv-resume-stream",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:conv-resume-stream",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    original_detached_streaming_response = server_app_module._detached_streaming_response

    def start_detached_stream_and_return_accepted(source, *, invocation_id=None, **kwargs):
        original_detached_streaming_response(source, invocation_id=invocation_id, **kwargs)
        return Response(status_code=202)

    monkeypatch.setattr(
        server_app_module,
        "_detached_streaming_response",
        start_detached_stream_and_return_accepted,
    )

    resume_input = {
        "type": "agentengine.resume_checkpoint",
        "run_id": "run-1",
        "checkpoint_id": "ckpt-1",
        "resume_attempt_id": "resume-1",
        "framework": "langgraph",
        "framework_ref": {
            "langgraph": {
                "thread_id": "forged-client-thread",
                "checkpoint_id": "forged-client-checkpoint",
            }
        },
    }
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first_response = await client.post(
            "/v1/responses",
            json={
                "input": [resume_input],
                "conversation": "conv-resume-stream",
                "safety_identifier": "user-a",
                "metadata": {"agentengine": {"invocation_id": "responses-resume-1"}},
                "stream": True,
            },
        )
        second_response = await client.post(
            "/v1/responses",
            json={
                "input": [{**resume_input, "resume_attempt_id": "resume-2"}],
                "conversation": "conv-resume-stream",
                "safety_identifier": "user-a",
                "metadata": {"agentengine": {"invocation_id": "responses-resume-2"}},
                "stream": True,
            },
        )

    assert first_response.status_code == 202
    assert second_response.status_code == 409
    detail = second_response.json()["detail"]
    assert detail["code"] == "resume_already_running"
    # 409 detail 字段契约：snake_case（与 checkpoint_not_resumable 对齐）
    assert isinstance(detail["session_id"], str) and detail["session_id"]
    assert isinstance(detail["invocation_id"], str) and detail["invocation_id"]
    assert isinstance(detail["run_id"], str) and detail["run_id"]


@pytest.mark.asyncio
async def test_run_agent_responses_checkpoint_resume_resolves_framework_ref_from_server(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-runagent-resume")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-runagent-resume",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-runagent-resume",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-runagent-resume",
                "UserId": "user-1",
                "ApiFormat": "responses",
                "Stream": False,
                "ResponsesInput": [
                    {
                        "type": "agentengine.resume_checkpoint",
                        "run_id": "run-1",
                        "checkpoint_id": "ckpt-1",
                        "resume_attempt_id": "resume-1",
                        "framework": "langgraph",
                        "framework_ref": {
                            "langgraph": {
                                "thread_id": "forged-client-thread",
                                "checkpoint_id": "forged-client-checkpoint",
                            }
                        },
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["metadata"]["agentengine"]["run_id"] == "run-1"
    assert (
        payload["metadata"]["agentengine"]["framework_ref"]["langgraph"]["thread_id"]
        == "tenant:agent:sess-runagent-resume"
    )
    assert runner.calls[-1]["framework_ref"]["langgraph"]["thread_id"] == "tenant:agent:sess-runagent-resume"


@pytest.mark.asyncio
async def test_run_agent_stream_checkpoint_resume_rejects_concurrent_resume_for_same_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-runagent-resume-concurrent",
    )
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-runagent-resume-concurrent",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-runagent-resume-concurrent",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    original_detached_streaming_response = server_app_module._detached_streaming_response

    def start_detached_stream_and_return_accepted(source, *, invocation_id=None, **kwargs):
        original_detached_streaming_response(source, invocation_id=invocation_id, **kwargs)
        return Response(status_code=202)

    monkeypatch.setattr(
        server_app_module,
        "_detached_streaming_response",
        start_detached_stream_and_return_accepted,
    )

    resume_input = [
        {
            "type": "agentengine.resume_checkpoint",
            "run_id": "run-1",
            "checkpoint_id": "ckpt-1",
            "resume_attempt_id": "resume-1",
            "framework": "langgraph",
            "framework_ref": {
                "langgraph": {
                    "thread_id": "forged-client-thread",
                    "checkpoint_id": "forged-client-checkpoint",
                }
            },
        }
    ]
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first_response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-runagent-resume-concurrent",
                "UserId": "user-1",
                "ApiFormat": "responses",
                "Stream": True,
                "InvocationId": "runagent-resume-1",
                "ResponsesInput": resume_input,
            },
        )
        second_response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-runagent-resume-concurrent",
                "UserId": "user-1",
                "ApiFormat": "responses",
                "Stream": True,
                "InvocationId": "runagent-resume-2",
                "ResponsesInput": resume_input,
            },
        )

    assert first_response.status_code == 202
    assert second_response.status_code == 409
    assert second_response.json()["detail"]["code"] == "resume_already_running"


@pytest.mark.asyncio
async def test_responses_accepts_official_conversation_object(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": {"id": "conv-object"},
                "safety_identifier": "user-object",
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["session_id"] == "conv-object"
    session = await service.get_session("conv-object")
    assert session is not None
    assert session.user_id == "user-object"


@pytest.mark.asyncio
async def test_responses_uses_agentengine_metadata_invocation_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-invocation",
                "metadata": {"agentengine": {"invocation_id": "run-known-invocation"}},
                "stream": False,
            },
        )

    assert response.status_code == 200
    events = await service.get_events("conv-invocation")
    assert events[0].invocation_id == "run-known-invocation"


@pytest.mark.asyncio
async def test_stream_responses_uses_agentengine_metadata_invocation_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        async with client.stream(
            "POST",
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-stream-invocation",
                "metadata": {"agentengine": {"invocation_id": "run-known-stream"}},
                "stream": True,
            },
        ) as response:
            chunks = []
            assert response.status_code == 200
            async for _line in response.aiter_lines():
                chunks.append(_line)

    events = await service.get_events("conv-stream-invocation")
    assert events, chunks
    assert events[0].invocation_id == "run-known-stream"


@pytest.mark.asyncio
async def test_stream_responses_registers_invocation_for_cancel_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()
    invocation_id = "run-responses-cancel"
    captured_invocations: list[str | None] = []

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    def fake_detached_streaming_response(source, *, invocation_id=None, **kwargs):
        del kwargs
        captured_invocations.append(invocation_id)
        return Response(status_code=202)

    monkeypatch.setattr(server_app_module, "_detached_streaming_response", fake_detached_streaming_response)
    response = await server_app_module.responses(
        server_app_module.ResponsesRequest(
            input="hello",
            conversation="conv-stream-cancel",
            metadata={"agentengine": {"invocation_id": invocation_id}},
            stream=True,
        )
    )

    assert response.status_code == 202
    assert captured_invocations == [invocation_id]


@pytest.mark.asyncio
async def test_responses_uses_deprecated_user_when_safety_identifier_missing(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-user",
                "user": "deprecated-user",
                "stream": False,
            },
        )

    assert response.status_code == 200
    session = await service.get_session("conv-user")
    assert session is not None
    assert session.user_id == "deprecated-user"


@pytest.mark.asyncio
async def test_stream_responses_user_and_account_reach_platform_context(monkeypatch):
    service = InMemorySessionService()
    runner = _DummyRunner()

    chunks = [
        chunk
        async for chunk in conversation.stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="ui-user-1",
            messages=[{"role": "user", "content": "hello"}],
            session_id="sess-hosted-stream",
            model=None,
            account_id="acct-1",
            prepare_runner=lambda _runner, _model: None,
            session_service_provider=lambda: service,
        )
    ]

    session = await service.get_session("sess-hosted-stream")
    assert session is not None
    assert session.user_id == "ui-user-1"
    assert runner.calls, chunks
    platform_context = runner.calls[-1]["platform_context"]
    assert platform_context["user_id"] == "ui-user-1"
    assert platform_context["account_id"] == "acct-1"
    assert platform_context["session_id"] == "sess-hosted-stream"


@pytest.mark.asyncio
async def test_responses_rejects_conflicting_conversation_and_legacy_session_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-a",
                "session_id": "legacy-b",
                "stream": False,
            },
        )

    assert response.status_code == 400
    assert "conversation" in response.text
    assert "session_id" in response.text


@pytest.mark.asyncio
async def test_responses_rejects_conversation_with_previous_response_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-a",
                "previous_response_id": "resp_previous",
                "stream": False,
            },
        )

    assert response.status_code == 400
    assert "conversation" in response.text
    assert "previous_response_id" in response.text


@pytest.mark.asyncio
async def test_responses_legacy_session_id_still_works(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "session_id": "legacy-session",
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["session_id"] == "legacy-session"
    session = await service.get_session("legacy-session")
    assert session is not None
    assert session.user_id == "user"


@pytest.mark.asyncio
async def test_responses_events_are_visible_through_runtime_local_list_session_events(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        run_response = await client.post(
            "/v1/responses",
            json={
                "input": "hello",
                "conversation": "conv-events",
                "safety_identifier": "user-events",
                "stream": False,
            },
        )
        events_response = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={"SessionId": "conv-events"},
        )

    assert run_response.status_code == 200
    assert events_response.status_code == 200
    events = events_response.json()["Data"]["Events"]
    message_events = [event for event in events if event["EventType"] in {"user_message", "assistant_message"}]
    assert [event["Author"] for event in message_events] == ["user", "demo-agent"]
    assert message_events[0]["Content"]["parts"][0]["text"] == "hello"
    assert message_events[1]["Content"]["parts"][0]["text"] == "assistant says hi"


@pytest.mark.asyncio
async def test_runtime_local_list_session_events_returns_total_and_page(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-events-page",
    )
    for index in range(4):
        await service.append_event(
            "sess-events-page",
            SessionEvent(
                author="user",
                event_type="user_message",
                content={"index": index},
            ),
        )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={
                "SessionId": "sess-events-page",
                "Offset": 0,
                "Limit": 2,
            },
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Offset"] == 0
    assert data["Limit"] == 2
    assert data["Total"] == 4
    assert [event["SeqId"] for event in data["Events"]] == [3, 4]


@pytest.mark.asyncio
async def test_runtime_local_list_session_events_filters_by_after_seq_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-events-after",
    )
    for index in range(4):
        await service.append_event(
            "sess-events-after",
            SessionEvent(
                author="user",
                event_type="user_message",
                content={"index": index},
            ),
        )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={
                "SessionId": "sess-events-after",
                "AfterSeqId": 2,
            },
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert [event["SeqId"] for event in data["Events"]] == [3, 4]
    assert data["Total"] == 2
    assert data["AfterSeqId"] == 2


@pytest.mark.asyncio
async def test_runtime_local_list_session_events_filters_by_before_seq_id(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-events-before",
    )
    for index in range(5):
        await service.append_event(
            "sess-events-before",
            SessionEvent(
                author="user",
                event_type="user_message",
                content={"index": index},
            ),
        )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={
                "SessionId": "sess-events-before",
                "BeforeSeqId": 4,
                "Limit": 2,
            },
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert [event["SeqId"] for event in data["Events"]] == [2, 3]
    assert data["Total"] == 3
    assert data["BeforeSeqId"] == 4


@pytest.mark.asyncio
async def test_list_session_checkpoints_filters_by_agent_session_and_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _DummyRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-checkpoints")
    await service.create_session(agent_id="other-agent", user_id="user-1", session_id="sess-other")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-checkpoints",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-checkpoints", "checkpoint_id": "ckpt-1"}},
        phase="tool_result",
        invocation_id="inv-1",
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-checkpoints",
        author="demo-agent",
        run_id="run-2",
        checkpoint_id="ckpt-2",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-checkpoints", "checkpoint_id": "ckpt-2"}},
        phase="completed",
        invocation_id="inv-2",
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-other",
        author="other-agent",
        run_id="run-1",
        checkpoint_id="ckpt-other",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:other:sess-other", "checkpoint_id": "ckpt-other"}},
        invocation_id="inv-other",
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "demo-agent", "SessionId": "sess-checkpoints", "RunId": "run-1"},
        )
        wrong_agent = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "other-agent", "SessionId": "sess-checkpoints"},
        )

    assert response.status_code == 200
    checkpoints = response.json()["Data"]["Checkpoints"]
    assert [item["CheckpointId"] for item in checkpoints] == ["ckpt-1"]
    assert checkpoints[0]["RunId"] == "run-1"
    assert checkpoints[0]["Framework"] == "langgraph"
    assert checkpoints[0]["FrameworkRef"]["langgraph"]["thread_id"] == "tenant:agent:sess-checkpoints"
    assert wrong_agent.status_code == 404


@pytest.mark.asyncio
async def test_list_session_checkpoints_returns_business_resume_fields(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-business-checkpoints")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-business-checkpoints",
        author="demo-agent",
        run_id="run-business",
        checkpoint_id="ckpt-metrics",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-business-checkpoints", "checkpoint_id": "ckpt-metrics"}},
        phase="指标聚合已完成，等待生成报告",
        invocation_id="inv-business",
        metadata={
            "stage": "清洗聚合指标",
            "summary": "GMV、转化率和退款率已经聚合完成",
            "next_action": "继续生成复盘报告",
            "status": "completed",
        },
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "demo-agent", "SessionId": "sess-business-checkpoints"},
        )

    assert response.status_code == 200
    checkpoint = response.json()["Data"]["Checkpoints"][0]
    assert checkpoint["RunId"] == "run-business"
    assert checkpoint["CheckpointId"] == "ckpt-metrics"
    assert checkpoint["Phase"] == "指标聚合已完成，等待生成报告"
    assert checkpoint["Stage"] == "清洗聚合指标"
    assert checkpoint["Summary"] == "GMV、转化率和退款率已经聚合完成"
    assert checkpoint["NextAction"] == "继续生成复盘报告"
    assert checkpoint["Status"] == "completed"
    assert checkpoint["IsResumable"] is None
    assert checkpoint["ResumeStatus"] == "unknown"
    assert checkpoint["ResumeDisabledReason"] == ""
    assert checkpoint["IsTerminal"] is False
    assert checkpoint["Backend"] == "unknown"
    assert checkpoint["Scope"] == "unknown"
    assert checkpoint["Durable"] is False


@pytest.mark.asyncio
async def test_list_session_checkpoints_filters_resumable_and_framework(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-filter-checkpoints")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-filter-checkpoints",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-resumable",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "sess-filter-checkpoints",
                "checkpoint_id": "ckpt-resumable",
                "next_node": "search",
            }
        },
        metadata={"is_resumable": True, "backend": "postgres", "scope": "shared", "durable": True},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-filter-checkpoints",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-terminal",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "sess-filter-checkpoints",
                "checkpoint_id": "ckpt-terminal",
                "next_node": "",
            }
        },
        metadata={"is_terminal": True, "is_resumable": False},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-filter-checkpoints",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-adk",
        framework="adk",
        framework_ref={"adk": {"session_id": "sess-filter-checkpoints"}},
        metadata={"is_resumable": False},
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-filter-checkpoints",
                "Framework": "langgraph",
                "OnlyResumable": True,
            },
        )

    assert response.status_code == 200
    checkpoints = response.json()["Data"]["Checkpoints"]
    assert [item["CheckpointId"] for item in checkpoints] == ["ckpt-resumable"]
    assert checkpoints[0]["IsResumable"] is True
    assert checkpoints[0]["ResumeStatus"] == "resumable"
    assert checkpoints[0]["NextNode"] == "search"
    assert checkpoints[0]["Backend"] == "postgres"


@pytest.mark.asyncio
async def test_list_session_checkpoints_is_the_single_checkpoint_listing_endpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-list-checkpoints")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    for index in range(3):
        await conversation_runtime.append_run_checkpoint_event(
            session_id="sess-list-checkpoints",
            author="demo-agent",
            run_id="run-page",
            checkpoint_id=f"ckpt-{index}",
            framework="langgraph",
            framework_ref={
                "langgraph": {
                    "thread_id": "sess-list-checkpoints",
                    "checkpoint_id": f"ckpt-{index}",
                    "next_node": "stage",
                }
            },
            metadata={
                "is_resumable": True,
                "backend": "postgres",
                "scope": "shared",
                "durable": True,
                "stage_index": index + 1,
                "total_stages": 3,
            },
            session_service_provider=lambda: service,
        )

    payload = {
        "AgentId": "demo-agent",
        "SessionId": "sess-list-checkpoints",
        "RunId": "run-page",
        "Offset": 1,
        "Limit": 1,
    }
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        removed_response = await client.post("/agentengine/api/v1/ListCheckpoints", json=payload)
        response = await client.post("/agentengine/api/v1/ListSessionCheckpoints", json=payload)
        removed_preview_response = await client.post(
            "/agentengine/api/v1/PreviewCheckpointResume",
            json={**payload, "CheckpointId": "ckpt-1"},
        )

    assert removed_response.status_code in {404, 405}
    assert removed_preview_response.status_code in {404, 405}
    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Total"] == 3
    assert data["Offset"] == 1
    assert data["Limit"] == 1
    assert [item["CheckpointId"] for item in data["Checkpoints"]] == ["ckpt-1"]
    checkpoint = data["Checkpoints"][0]
    assert checkpoint["StageIndex"] == 2
    assert checkpoint["TotalStages"] == 3
    assert checkpoint["IsResumable"] is True
    assert checkpoint["CreatedAt"]


@pytest.mark.asyncio
async def test_list_session_checkpoints_includes_resume_audit_fields(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-checkpoint-audit")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-checkpoint-audit",
        author="demo-agent",
        run_id="run-audit",
        checkpoint_id="ckpt-audit",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "sess-checkpoint-audit",
                "checkpoint_id": "ckpt-audit",
                "next_node": "report",
            }
        },
        metadata={
            "is_resumable": True,
            "backend": "postgres",
            "scope": "shared",
            "durable": True,
            "expires_at": "2099-06-30T12:00:00Z",
            "checkpoint_status": "active",
        },
        session_service_provider=lambda: service,
    )
    for attempt_id in ("resume-1", "resume-2"):
        await conversation_runtime.append_run_resume_event(
            session_id="sess-checkpoint-audit",
            author="demo-agent",
            run_id="run-audit",
            checkpoint_id="ckpt-audit",
            resume_attempt_id=attempt_id,
            framework="langgraph",
            framework_ref={
                "langgraph": {
                    "thread_id": "sess-checkpoint-audit",
                    "checkpoint_id": "ckpt-audit",
                }
            },
            session_service_provider=lambda: service,
        )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "demo-agent", "SessionId": "sess-checkpoint-audit"},
        )
        preview_response = await client.post(
            "/agentengine/api/v1/GetCheckpointResumePreview",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-checkpoint-audit",
                "RunId": "run-audit",
                "CheckpointId": "ckpt-audit",
            },
        )

    assert response.status_code == 200
    checkpoint = response.json()["Data"]["Checkpoints"][0]
    assert checkpoint["ResumeCount"] == 2
    assert checkpoint["LastResumedAt"]
    assert checkpoint["ReplayAllowed"] is True
    assert checkpoint["ExpiresAt"] == "2099-06-30T12:00:00Z"
    assert checkpoint["CheckpointStatus"] == "resumed"
    assert checkpoint["Metadata"]["resume_count"] == 2
    assert checkpoint["Metadata"]["last_resumed_at"] == checkpoint["LastResumedAt"]
    assert preview_response.status_code == 200
    preview_checkpoint = preview_response.json()["Data"]["Preview"]["Checkpoint"]
    assert preview_checkpoint["ResumeCount"] == 2
    assert preview_checkpoint["LastResumedAt"] == checkpoint["LastResumedAt"]


@pytest.mark.asyncio
async def test_list_session_checkpoints_disables_expired_or_non_replayable_checkpoints(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-checkpoint-policy")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-checkpoint-policy",
        author="demo-agent",
        run_id="run-policy",
        checkpoint_id="ckpt-no-replay",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-checkpoint-policy", "checkpoint_id": "ckpt-no-replay"}},
        metadata={"is_resumable": True, "replay_allowed": False},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_resume_event(
        session_id="sess-checkpoint-policy",
        author="demo-agent",
        run_id="run-policy",
        checkpoint_id="ckpt-no-replay",
        resume_attempt_id="resume-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-checkpoint-policy", "checkpoint_id": "ckpt-no-replay"}},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-checkpoint-policy",
        author="demo-agent",
        run_id="run-policy",
        checkpoint_id="ckpt-expired",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-checkpoint-policy", "checkpoint_id": "ckpt-expired"}},
        metadata={"is_resumable": True, "expires_at": "2000-01-01T00:00:00Z"},
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "demo-agent", "SessionId": "sess-checkpoint-policy"},
        )

    assert response.status_code == 200
    checkpoints = {
        item["CheckpointId"]: item
        for item in response.json()["Data"]["Checkpoints"]
    }
    no_replay = checkpoints["ckpt-no-replay"]
    assert no_replay["ReplayAllowed"] is False
    assert no_replay["ResumeCount"] == 1
    assert no_replay["IsResumable"] is False
    assert no_replay["ResumeStatus"] == "disabled"
    assert no_replay["CheckpointStatus"] == "resumed"
    assert "重复恢复" in no_replay["ResumeDisabledReason"]
    expired = checkpoints["ckpt-expired"]
    assert expired["IsResumable"] is False
    assert expired["ResumeStatus"] == "disabled"
    assert expired["CheckpointStatus"] == "expired"
    assert "已过期" in expired["ResumeDisabledReason"]


@pytest.mark.asyncio
async def test_resume_run_rejects_checkpoint_policy_disabled_by_audit(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-policy")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-policy",
        author="demo-agent",
        run_id="run-policy",
        checkpoint_id="ckpt-no-replay",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-resume-policy", "checkpoint_id": "ckpt-no-replay"}},
        metadata={"is_resumable": True, "replay_allowed": False},
        session_service_provider=lambda: service,
    )
    await conversation_runtime.append_run_resume_event(
        session_id="sess-resume-policy",
        author="demo-agent",
        run_id="run-policy",
        checkpoint_id="ckpt-no-replay",
        resume_attempt_id="resume-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-resume-policy", "checkpoint_id": "ckpt-no-replay"}},
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-policy",
                "RunId": "run-policy",
                "CheckpointId": "ckpt-no-replay",
                "ResumeAttemptId": "resume-2",
                "Stream": False,
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "checkpoint_not_resumable"
    assert detail["resume_status"] == "disabled"
    assert "重复恢复" in detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_resume_run_rejects_expired_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-expired")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-expired",
        author="demo-agent",
        run_id="run-expired",
        checkpoint_id="ckpt-expired",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-resume-expired", "checkpoint_id": "ckpt-expired"}},
        metadata={"is_resumable": True, "expires_at": "2000-01-01T00:00:00Z"},
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-expired",
                "RunId": "run-expired",
                "CheckpointId": "ckpt-expired",
                "ResumeAttemptId": "resume-expired",
                "Stream": False,
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "checkpoint_not_resumable"
    assert detail["resume_status"] == "disabled"
    assert "已过期" in detail["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_resume_run_action_reuses_checkpoint_and_records_resume(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-action")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-action",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-resume-action", "checkpoint_id": "ckpt-1"}},
        invocation_id="inv-1",
        metadata={
            "stage_key": "plan_research",
            "stage_index": 1,
            "total_stages": 7,
            "artifact_preview": {"path": "research/x/01-plan.json", "kind": "json"},
        },
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-action",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-1",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()["Data"]
    assert payload["session_id"] == "sess-resume-action"
    assert payload["metadata"]["agentengine"]["run_id"] == "run-1"
    assert runner.calls[-1]["checkpoint_resume"] is True
    assert runner.calls[-1]["framework_ref"]["langgraph"]["checkpoint_id"] == "ckpt-1"
    assert runner.calls[-1]["metadata"]["stage_key"] == "plan_research"
    assert runner.calls[-1]["metadata"]["stage_index"] == 1
    assert runner.calls[-1]["metadata"]["total_stages"] == 7
    assert runner.calls[-1]["metadata"]["artifact_preview"]["path"] == "research/x/01-plan.json"
    events = await service.get_events("sess-resume-action")
    resume_events = [event for event in events if event.event_type == "run_resume"]
    assert len(resume_events) == 1
    assert resume_events[0].metadata["resume_attempt_id"] == "resume-1"


@pytest.mark.asyncio
async def test_resume_run_action_returns_noop_for_terminal_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-disabled")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-disabled",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-terminal",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-resume-disabled", "checkpoint_id": "ckpt-terminal"}},
        metadata={
            "is_terminal": True,
            "is_resumable": False,
            "resume_disabled_reason": "该 checkpoint 已是终态；请选择更早恢复点重跑",
        },
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-disabled",
                "RunId": "run-1",
                "CheckpointId": "ckpt-terminal",
                "ResumeAttemptId": "resume-disabled",
            },
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["status"] == "noop"
    assert data["Reason"]
    assert runner.calls == []
    events = await service.get_events("sess-resume-disabled")
    assert [event.event_type for event in events if event.event_type in {"run_resume", "run_status"}] == [
        "run_resume",
        "run_status",
    ]
    assert events[-1].metadata["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_run_action_rejects_process_local_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-local")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-local",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-local",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-resume-local", "checkpoint_id": "ckpt-local"}},
        metadata={
            "is_resumable": False,
            "backend": "memory",
            "scope": "process_local",
            "resume_disabled_reason": "进程内 checkpoint 不能跨实例恢复",
        },
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-local",
                "RunId": "run-1",
                "CheckpointId": "ckpt-local",
                "ResumeAttemptId": "resume-local",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "checkpoint_not_resumable"
    assert "进程内" in response.json()["detail"]["reason"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_get_checkpoint_resume_preview_reports_terminal_checkpoint_as_disabled(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-preview-terminal")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-preview-terminal",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-terminal",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "sess-preview-terminal", "checkpoint_id": "ckpt-terminal"}},
        metadata={"is_terminal": True, "is_resumable": False},
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetCheckpointResumePreview",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-preview-terminal",
                "RunId": "run-1",
                "CheckpointId": "ckpt-terminal",
            },
        )

    assert response.status_code == 200
    preview = response.json()["Data"]["Preview"]
    assert preview["CanResume"] is False
    assert preview["ExpectedAction"] == "disabled"
    assert "终态" in preview["Reason"]


@pytest.mark.asyncio
async def test_resume_run_action_stream_uses_invocation_id_for_detached_cancel(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-stream")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-stream",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-resume-stream", "checkpoint_id": "ckpt-1"}},
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    captured_invocations: list[str | None] = []

    def fake_detached_streaming_response(source, *, invocation_id=None, **kwargs):
        del kwargs
        captured_invocations.append(invocation_id)
        return Response(status_code=202)

    monkeypatch.setattr(server_app_module, "_detached_streaming_response", fake_detached_streaming_response)
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-stream",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-1",
                "InvocationId": "run-ui-resume-1",
                "Stream": True,
            },
        )

    assert response.status_code == 202
    assert captured_invocations == ["run-ui-resume-1"]


@pytest.mark.asyncio
async def test_resume_run_action_stream_passes_checkpoint_metadata_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-stream-metadata")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-stream-metadata",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-resume-stream-metadata", "checkpoint_id": "ckpt-1"}},
        invocation_id="inv-checkpoint",
        metadata={"stage_key": "plan_research", "stage_index": 1, "total_stages": 7},
        session_service_provider=lambda: service,
    )

    async def consume_stream(source):
        async for _chunk in source:
            pass

    def fake_detached_streaming_response(source, *, invocation_id=None, **kwargs):
        del invocation_id, kwargs
        return Response(status_code=202, background=BackgroundTask(consume_stream, source))

    monkeypatch.setattr(server_app_module, "_detached_streaming_response", fake_detached_streaming_response)
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-stream-metadata",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-1",
                "InvocationId": "run-ui-resume-1",
                "Stream": True,
            },
        )

    assert response.status_code == 202
    assert runner.calls[-1]["checkpoint_resume"] is True
    assert runner.calls[-1]["metadata"]["stage_key"] == "plan_research"
    assert runner.calls[-1]["metadata"]["stage_index"] == 1


@pytest.mark.asyncio
async def test_resume_run_action_stream_registers_detached_cancel(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-cancel")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-cancel",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-resume-cancel",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    invocation_id = "run-ui-resume-cancel"
    original_detached_streaming_response = server_app_module._detached_streaming_response

    def start_detached_stream_and_return_accepted(source, *, invocation_id=None, **kwargs):
        original_detached_streaming_response(source, invocation_id=invocation_id, **kwargs)
        return Response(status_code=202)

    monkeypatch.setattr(
        server_app_module,
        "_detached_streaming_response",
        start_detached_stream_and_return_accepted,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resume_response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-cancel",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-1",
                "InvocationId": invocation_id,
                "Stream": True,
            },
        )

        for _ in range(20):
            events = await service.get_events("sess-resume-cancel")
            statuses = [
                event.content.get("status")
                for event in events
                if event.event_type == "run_status"
            ]
            if "in_progress" in statuses and "cancelled" not in statuses:
                break
            await asyncio.sleep(0.02)

        cancel_response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": invocation_id},
        )

    assert resume_response.status_code == 202
    assert cancel_response.status_code == 200
    cancel_data = cancel_response.json()["Data"]
    assert cancel_data["Found"] is True
    assert cancel_data["Cancelled"] is True
    assert cancel_data["RunnerCancelStatus"] == "accepted"

    for _ in range(20):
        events = await service.get_events("sess-resume-cancel")
        statuses = [
            event.content.get("status")
            for event in events
            if event.event_type == "run_status"
        ]
        if "cancelled" in statuses:
            break
        await asyncio.sleep(0.02)

    events = await service.get_events("sess-resume-cancel")
    statuses = [
        event.content.get("status")
        for event in events
        if event.event_type == "run_status"
    ]
    # resume 现在先写 run_status(resuming) 再写 in_progress，cancel 后写 cancelled。
    assert statuses == ["resuming", "in_progress", "cancelled"]
    assert runner.cancel_requests == [invocation_id]


@pytest.mark.asyncio
async def test_resume_run_action_stream_rejects_concurrent_resume_for_same_run(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-resume-concurrent",
    )
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-resume-concurrent",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-resume-concurrent",
                "checkpoint_id": "ckpt-1",
            }
        },
        invocation_id="inv-checkpoint",
        session_service_provider=lambda: service,
    )

    original_detached_streaming_response = server_app_module._detached_streaming_response

    def start_detached_stream_and_return_accepted(source, *, invocation_id=None, **kwargs):
        original_detached_streaming_response(source, invocation_id=invocation_id, **kwargs)
        return Response(status_code=202)

    monkeypatch.setattr(
        server_app_module,
        "_detached_streaming_response",
        start_detached_stream_and_return_accepted,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first_response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-concurrent",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-1",
                "InvocationId": "run-ui-resume-1",
                "Stream": True,
            },
        )
        second_response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-concurrent",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
                "ResumeAttemptId": "resume-2",
                "InvocationId": "run-ui-resume-2",
                "Stream": True,
            },
        )

    assert first_response.status_code == 202
    assert second_response.status_code == 409
    assert second_response.json()["detail"]["code"] == "resume_already_running"


@pytest.mark.asyncio
async def test_resume_run_action_rejects_unknown_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-resume-missing")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ResumeRun",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-resume-missing",
                "RunId": "run-unknown",
                "CheckpointId": "ckpt-unknown",
                "Stream": False,
            },
        )

    assert response.status_code == 404
    assert runner.calls == []
    events = await service.get_events("sess-resume-missing")
    assert events == []


@pytest.mark.asyncio
async def test_append_run_checkpoint_event_deduplicates_same_checkpoint(monkeypatch):
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()

    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-dedupe-checkpoint",
    )

    first = await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-dedupe-checkpoint",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-dedupe-checkpoint",
                "checkpoint_id": "ckpt-1",
            }
        },
        phase="runner",
        invocation_id="inv-runner",
        session_service_provider=lambda: service,
    )
    second = await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-dedupe-checkpoint",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={
            "langgraph": {
                "thread_id": "tenant:agent:sess-dedupe-checkpoint",
                "checkpoint_id": "ckpt-1",
            }
        },
        phase="stream",
        invocation_id="inv-framework",
        session_service_provider=lambda: service,
    )

    events = await service.get_events("sess-dedupe-checkpoint")
    checkpoint_events = [event for event in events if event.event_type == "run_checkpoint"]
    assert len(checkpoint_events) == 1
    assert second.id == first.id


@pytest.mark.asyncio
async def test_get_checkpoint_resume_preview_summarizes_checkpoint_and_tool_receipts(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    conversation_runtime = importlib.import_module("ksadk.conversations.runtime")
    service = InMemorySessionService()
    runner = _CheckpointResumeRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-preview")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    await service.append_event(
        "sess-preview",
        SessionEvent(
            id="evt-tool",
            author="tool",
            event_type="tool_result",
            content={"role": "user", "parts": [{"text": "{'ok': True}"}]},
            metadata={
                "tool_name": "write_workspace_file",
                "tool_args": {"path": "notes.txt", "content": "hello"},
                "tool_output": {"ok": True, "path": "notes.txt"},
                "run_id": "run-1",
                "tool_receipt": {
                    "receipt_id": "tr_1",
                    "idempotency_key": "tool_receipt:abc",
                    "tool_name": "write_workspace_file",
                    "tool_call_id": "call_write",
                    "run_id": "run-1",
                    "checkpoint_id": "",
                    "status": "completed",
                    "created_at": 10.0,
                },
            },
            invocation_id="inv-tool",
        ),
    )
    await conversation_runtime.append_run_checkpoint_event(
        session_id="sess-preview",
        author="demo-agent",
        run_id="run-1",
        checkpoint_id="ckpt-1",
        framework="langgraph",
        framework_ref={"langgraph": {"thread_id": "tenant:agent:sess-preview", "checkpoint_id": "ckpt-1"}},
        phase="tool_result",
        invocation_id="inv-1",
        session_service_provider=lambda: service,
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetCheckpointResumePreview",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-preview",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
            },
        )

    assert response.status_code == 200
    preview = response.json()["Data"]["Preview"]
    assert preview["Checkpoint"]["CheckpointId"] == "ckpt-1"
    assert preview["Capabilities"]["CheckpointResume"] is True
    assert preview["Risk"]["Level"] == "medium"
    assert preview["Risk"]["DuplicateSideEffectRisk"] is True
    assert preview["ToolReceipts"][0]["ToolName"] == "write_workspace_file"
    assert preview["ToolReceipts"][0]["Status"] == "completed"


@pytest.mark.asyncio
async def test_list_tool_receipts_filters_by_agent_session_run_and_checkpoint(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-receipts")
    await service.create_session(agent_id="other-agent", user_id="user-1", session_id="sess-other-receipts")
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    await service.append_event(
        "sess-receipts",
        SessionEvent(
            id="evt-receipt-1",
            author="tool",
            event_type="tool_result",
            content={"role": "user", "parts": [{"text": "{'ok': True}"}]},
            metadata={
                "tool_name": "write_workspace_file",
                "run_id": "run-1",
                "tool_receipt": {
                    "receipt_id": "tr_1",
                    "idempotency_key": "tool_receipt:1",
                    "tool_name": "write_workspace_file",
                    "tool_call_id": "call-1",
                    "run_id": "run-1",
                    "checkpoint_id": "ckpt-1",
                    "status": "completed",
                    "replayed": False,
                },
            },
            invocation_id="inv-1",
        ),
    )
    await service.append_event(
        "sess-receipts",
        SessionEvent(
            id="evt-receipt-2",
            author="tool",
            event_type="tool_result",
            content={"role": "user", "parts": [{"text": "{'ok': True}"}]},
            metadata={
                "tool_name": "send_notification",
                "run_id": "run-2",
                "tool_receipt": {
                    "receipt_id": "tr_2",
                    "idempotency_key": "tool_receipt:2",
                    "tool_name": "send_notification",
                    "tool_call_id": "call-2",
                    "run_id": "run-2",
                    "checkpoint_id": "ckpt-2",
                    "status": "completed",
                },
            },
            invocation_id="inv-2",
        ),
    )

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/ListToolReceipts",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-receipts",
                "RunId": "run-1",
                "CheckpointId": "ckpt-1",
            },
        )
        wrong_agent = await client.post(
            "/agentengine/api/v1/ListToolReceipts",
            json={"AgentId": "other-agent", "SessionId": "sess-receipts"},
        )

    assert response.status_code == 200
    receipts = response.json()["Data"]["ToolReceipts"]
    assert [receipt["ReceiptId"] for receipt in receipts] == ["tr_1"]
    assert receipts[0]["ToolName"] == "write_workspace_file"
    assert receipts[0]["RunId"] == "run-1"
    assert receipts[0]["CheckpointId"] == "ckpt-1"
    assert wrong_agent.status_code == 404


@pytest.mark.asyncio
async def test_run_agent_action_passes_model_options_to_runner(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "Stream": False,
                "Model": "glm-5.1",
                "ModelOptions": {"thinking": {"type": "disabled"}},
            },
        )

    assert response.status_code == 200
    assert runner.calls[-1]["model_options"] == {
        "thinking": {"type": "disabled"},
        "reasoning": {"effort": "none"},
        "max_reasoning_tokens": 0,
    }


@pytest.mark.asyncio
async def test_subscribe_run_events_streams_events_appended_after_subscription(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(_DummyRunner())

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-subscribe",
    )
    in_progress = await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "eventType": "run_status",
                "invocationId": "inv-live",
                "content": {"status": "in_progress"},
            },
            session_id=session.id,
        ),
    )

    async def append_later():
        await asyncio.sleep(0.02)
        await service.append_event(
            session.id,
            SessionEvent.from_dict(
                {
                    "author": "demo-agent",
                    "eventType": "assistant_message",
                    "invocationId": "inv-live",
                    "content": {"role": "model", "parts": [{"text": "hello"}]},
                },
                session_id=session.id,
            ),
        )
        await service.append_event(
            session.id,
            SessionEvent.from_dict(
                {
                    "author": "demo-agent",
                    "eventType": "run_status",
                    "invocationId": "inv-live",
                    "content": {"status": "completed"},
                },
                session_id=session.id,
            ),
        )

    task = asyncio.create_task(append_later())
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get(
            "/agentengine/api/v1/SubscribeRunEvents",
            params={
                "SessionId": session.id,
                "InvocationId": "inv-live",
                "AfterSeqId": str(in_progress.seq_id),
            },
        )
    await task

    assert response.status_code == 200
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    assert [payload["EventType"] for payload in payloads] == [
        "assistant_message",
        "run_status",
    ]
    assert payloads[0]["Content"]["parts"][0]["text"] == "hello"
    assert payloads[-1]["Content"]["status"] == "completed"


@pytest.mark.asyncio
async def test_subscribe_run_events_keeps_idle_reconnect_alive(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    monkeypatch.setattr(server_app_module, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0)
    server_app_module.set_runner(_DummyRunner())

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-subscribe-heartbeat",
    )
    in_progress = await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "eventType": "run_status",
                "invocationId": "inv-heartbeat",
                "content": {"status": "in_progress"},
            },
            session_id=session.id,
        ),
    )

    async def append_completed_later():
        await asyncio.sleep(0.01)
        await service.append_event(
            session.id,
            SessionEvent.from_dict(
                {
                    "author": "demo-agent",
                    "eventType": "run_status",
                    "invocationId": "inv-heartbeat",
                    "content": {"status": "completed"},
                },
                session_id=session.id,
            ),
        )

    task = asyncio.create_task(append_completed_later())
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.get(
            "/agentengine/api/v1/SubscribeRunEvents",
            params={
                "SessionId": session.id,
                "InvocationId": "inv-heartbeat",
                "AfterSeqId": str(in_progress.seq_id),
            },
        )
    await task

    assert response.status_code == 200
    assert ": heartbeat\n\n" in response.text
    assert '"status": "completed"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_subscribe_run_events_reconnects_without_replaying_consumed_events(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(_DummyRunner())

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-subscribe-reconnect",
    )
    in_progress = await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "eventType": "run_status",
                "invocationId": "inv-reconnect",
                "content": {"status": "in_progress"},
            },
            session_id=session.id,
        ),
    )
    assistant = await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "eventType": "assistant_message",
                "invocationId": "inv-reconnect",
                "content": {"role": "model", "parts": [{"text": "halfway"}]},
            },
            session_id=session.id,
        ),
    )
    await service.append_event(
        session.id,
        SessionEvent.from_dict(
            {
                "author": "demo-agent",
                "eventType": "run_status",
                "invocationId": "other-invocation",
                "content": {"status": "completed"},
            },
            session_id=session.id,
        ),
    )

    async def append_completed_later():
        await asyncio.sleep(0.02)
        await service.append_event(
            session.id,
            SessionEvent.from_dict(
                {
                    "author": "demo-agent",
                    "eventType": "run_status",
                    "invocationId": "inv-reconnect",
                    "content": {"status": "completed"},
                },
                session_id=session.id,
            ),
        )

    task = asyncio.create_task(append_completed_later())
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first_response = await client.get(
            "/agentengine/api/v1/SubscribeRunEvents",
            params={
                "SessionId": session.id,
                "InvocationId": "inv-reconnect",
                "AfterSeqId": 0,
            },
        )
        second_response = await client.get(
            "/agentengine/api/v1/SubscribeRunEvents",
            params={
                "SessionId": session.id,
                "InvocationId": "inv-reconnect",
                "AfterSeqId": str(assistant.seq_id),
            },
        )
    await task

    assert first_response.status_code == 200
    first_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in first_response.text.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    assert [payload["SeqId"] for payload in first_payloads[:2]] == [
        in_progress.seq_id,
        assistant.seq_id,
    ]

    assert second_response.status_code == 200
    second_payloads = [
        json.loads(line.removeprefix("data: "))
        for line in second_response.text.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    assert [payload["EventType"] for payload in second_payloads] == ["run_status"]
    assert second_payloads[0]["SeqId"] > assistant.seq_id
    assert second_payloads[0]["Content"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_agent_stream_continues_after_client_disconnect(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _SlowStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        async with client.stream(
            "POST",
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-detached-run",
                "Messages": [{"role": "user", "content": "hello"}],
                "Stream": True,
                "ApiFormat": "responses",
            },
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: ") and "response.created" in line:
                    break

    for _ in range(20):
        events = await service.get_events("sess-detached-run")
        if events and events[-1].event_type == "run_status" and events[-1].content.get("status") == "completed":
            break
        await asyncio.sleep(0.02)

    events = await service.get_events("sess-detached-run")
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert events[-2].content["parts"][0]["text"] == "hello"
    assert events[-1].content["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_run_cancels_detached_stream_and_writes_cancelled_status(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    invocation_id = "inv-cancel-detached"
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        server_app_module._detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=runner,
                agent_id="demo-agent",
                user_id="user",
                messages=[{"role": "user", "content": "hello"}],
                session_id="sess-cancel-run",
                model=None,
                prepare_runner=lambda _runner, _model: None,
                invocation_id=invocation_id,
                session_service_provider=lambda: service,
            ),
            invocation_id=invocation_id,
        )

        for _ in range(20):
            events = await service.get_events("sess-cancel-run")
            statuses = [
                event.content.get("status")
                for event in events
                if event.event_type == "run_status"
            ]
            if statuses == ["in_progress"]:
                break
            await asyncio.sleep(0.02)

        cancel_response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": invocation_id},
        )

    assert cancel_response.status_code == 200
    cancel_data = cancel_response.json()["Data"]
    assert cancel_data["Found"] is True
    assert cancel_data["Cancelled"] is True
    assert cancel_data["Status"] == "cancelling"

    for _ in range(20):
        events = await service.get_events("sess-cancel-run")
        if events and events[-1].event_type == "run_status" and events[-1].content.get("status") == "cancelled":
            break
        await asyncio.sleep(0.02)

    events = await service.get_events("sess-cancel-run")
    event_types = [event.event_type for event in events]
    statuses = [
        event.content.get("status")
        for event in events
        if event.event_type == "run_status"
    ]
    assert statuses == ["in_progress", "cancelled"]
    assert "assistant_message" not in event_types
    assert runner.cancel_requests == [invocation_id]


@pytest.mark.asyncio
async def test_delete_session_cancels_active_detached_stream_before_removing_session(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    invocation_id = "inv-delete-detached"
    session_id = "sess-delete-detached"
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        server_app_module._detached_streaming_response(
            conversation.stream_responses_conversation_turn(
                runner=runner,
                agent_id="demo-agent",
                user_id="user",
                messages=[{"role": "user", "content": "hello"}],
                session_id=session_id,
                model=None,
                prepare_runner=lambda _runner, _model: None,
                invocation_id=invocation_id,
                session_service_provider=lambda: service,
            ),
            invocation_id=invocation_id,
            session_id=session_id,
        )

        for _ in range(20):
            events = await service.get_events(session_id)
            if any(event.event_type == "run_status" for event in events):
                break
            await asyncio.sleep(0.02)

        response = await client.post(
            "/agentengine/api/v1/DeleteSession",
            json={"SessionId": session_id},
        )

    assert response.status_code == 200
    assert response.json()["Data"]["Deleted"] is True
    assert invocation_id not in server_app_module._DETACHED_STREAMS_BY_INVOCATION
    assert await service.get_session(session_id) is None


@pytest.mark.asyncio
async def test_cancel_run_reports_unsupported_when_runner_has_no_cancel_hook(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _OverrideStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": "inv-unsupported"},
        )

    assert response.status_code == 200
    cancel_data = response.json()["Data"]
    assert cancel_data["Found"] is False
    assert cancel_data["Cancelled"] is False
    assert cancel_data["Status"] == "unsupported"
    assert cancel_data["RunnerCancelStatus"] == "unsupported"
