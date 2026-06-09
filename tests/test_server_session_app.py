from __future__ import annotations

import base64
import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest, InlineData, Part
from ksadk.sessions.base import SessionEvent
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
            },
        )

    assert response.status_code == 200
    assert runner.prepared_models == ["glm-5.1"]
    assert runner.calls[-1]["model"] == "glm-5.1"


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
