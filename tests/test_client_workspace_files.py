from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.api import AttachmentContent
from ksadk.api.client import AgentEngineAPIError, AgentEngineClient


class _FakeRuntimeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_payload=None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._json_payload = json_payload
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self.text = content.decode("utf-8", errors="ignore")

    def json(self):
        if self._json_payload is None:
            raise json.JSONDecodeError("Expecting value", self.text or "", 0)
        return self._json_payload


class _FakeRuntimeSession:
    def __init__(self, responses: list[_FakeRuntimeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers"),
                "params": kwargs.get("params"),
                "files": kwargs.get("files"),
                "stream": kwargs.get("stream"),
            }
        )
        if not self._responses:
            raise AssertionError("unexpected runtime request")
        return self._responses.pop(0)


def test_attachment_content_is_exported_from_api_package():
    assert AttachmentContent.__name__ == "AttachmentContent"


def test_download_attachment_content_uses_signed_attachment_action(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[dict] = []

    def _fake_action_raw_request(
        method,
        action,
        *,
        params=None,
        accept="application/json",
        **kwargs,
    ):
        calls.append(
            {
                "method": method,
                "action": action,
                "params": params,
                "accept": accept,
                "extra": kwargs,
            }
        )
        return _FakeRuntimeResponse(
            content=b"# hosted",
            headers={
                "content-type": "text/markdown; charset=utf-8",
                "content-disposition": "inline; filename*=UTF-8''%E6%B5%8B%E8%AF%95.md",
            },
        )

    monkeypatch.setattr(client, "_action_raw_request", _fake_action_raw_request)

    content = client.download_attachment_content("ae-upload://hosted123.md")

    assert calls == [
        {
            "method": "GET",
            "action": "AttachmentContent",
            "params": {"FileUri": "ae-upload://hosted123.md"},
            "accept": "application/octet-stream",
            "extra": {},
        }
    ]
    assert content.data == b"# hosted"
    assert content.content_type == "text/markdown; charset=utf-8"
    assert content.display_name == "测试.md"


@pytest.mark.asyncio
async def test_list_workspace_files_uses_direct_runtime_endpoint(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    async def _fake_get_agent(**kwargs):
        assert kwargs == {"agent_id": "ar-demo", "name": None, "include_api_key": True}
        return {
            "basic": {"agent_id": "ar-demo", "name": "demo"},
            "quick_access": {
                "public_endpoint": "https://agent.example.com",
                "api_key": "ak-demo",
            },
        }

    session = _FakeRuntimeSession(
        [
            _FakeRuntimeResponse(
                json_payload={
                    "Root": "workspace",
                    "Path": "docs",
                    "Entries": [{"Name": "guide.md", "Path": "docs/guide.md", "Type": "file"}],
                }
            )
        ]
    )
    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_get_session", lambda: session)

    payload = await client.list_workspace_files(agent_id="ar-demo", path="docs", recursive=True)

    assert payload["path"] == "docs"
    assert payload["entries"][0]["path"] == "docs/guide.md"
    assert session.calls == [
        {
            "method": "GET",
            "url": "https://agent.example.com/_ksadk/workspace/v1/entries",
            "headers": {"Authorization": "Bearer ak-demo"},
            "params": {"path": "docs", "recursive": "true"},
            "files": None,
            "stream": False,
        }
    ]


@pytest.mark.asyncio
async def test_upload_download_and_delete_workspace_file_use_runtime_data_plane(
    monkeypatch,
    tmp_path: Path,
):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    async def _fake_get_agent(**kwargs):
        assert kwargs["include_api_key"] is True
        return {
            "basic": {"agent_id": "ar-demo", "name": "demo"},
            "quick_access": {
                "public_endpoint": "https://agent.example.com",
                "api_key": "ak-demo",
            },
        }

    local_file = tmp_path / "report.txt"
    local_file.write_text("workspace hello", encoding="utf-8")
    session = _FakeRuntimeSession(
        [
            _FakeRuntimeResponse(
                json_payload={
                    "Entry": {
                        "Name": "report.txt",
                        "Path": "reports/report.txt",
                        "Type": "file",
                        "SizeBytes": 15,
                    }
                }
            ),
            _FakeRuntimeResponse(
                content=b"workspace hello",
                headers={"content-type": "text/plain"},
            ),
            _FakeRuntimeResponse(json_payload={"Deleted": True}),
        ]
    )
    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_get_session", lambda: session)

    upload_payload = await client.upload_workspace_file(
        agent_id="ar-demo",
        remote_path="reports/report.txt",
        local_path=local_file,
    )
    download_payload = await client.download_workspace_file(
        agent_id="ar-demo",
        remote_path="reports/report.txt",
    )
    delete_payload = await client.delete_workspace_file(
        agent_id="ar-demo",
        remote_path="reports/report.txt",
    )

    assert upload_payload["entry"]["path"] == "reports/report.txt"
    assert download_payload == b"workspace hello"
    assert delete_payload["deleted"] is True
    assert delete_payload["transport_mode"] == "runtime_direct"
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == "https://agent.example.com/_ksadk/workspace/v1/files/reports/report.txt"
    assert session.calls[0]["headers"] == {"Authorization": "Bearer ak-demo"}
    assert session.calls[0]["files"] is not None
    assert session.calls[1] == {
        "method": "GET",
        "url": "https://agent.example.com/_ksadk/workspace/v1/files/reports/report.txt",
        "headers": {"Authorization": "Bearer ak-demo"},
        "params": None,
        "files": None,
        "stream": False,
    }
    assert session.calls[2] == {
        "method": "DELETE",
        "url": "https://agent.example.com/_ksadk/workspace/v1/files/reports/report.txt",
        "headers": {"Authorization": "Bearer ak-demo"},
        "params": None,
        "files": None,
        "stream": False,
    }


@pytest.mark.asyncio
async def test_list_workspace_files_surfaces_invalid_runtime_json_with_actionable_error(
    monkeypatch,
):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")

    async def _fake_get_agent(**kwargs):
        assert kwargs["include_api_key"] is True
        return {
            "basic": {"agent_id": "ar-demo", "name": "demo"},
            "quick_access": {
                "public_endpoint": "https://agent.example.com",
                "api_key": "ak-demo",
            },
        }

    session = _FakeRuntimeSession(
        [
            _FakeRuntimeResponse(
                content=b"",
                headers={"content-type": "application/json"},
            )
        ]
    )
    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_get_session", lambda: session)

    with pytest.raises(AgentEngineAPIError) as excinfo:
        await client.list_workspace_files(agent_id="ar-demo", path="docs")

    assert excinfo.value.code == 502
    assert "workspace runtime returned invalid JSON" in excinfo.value.message
    assert "https://agent.example.com/_ksadk/workspace/v1/entries" in excinfo.value.message


@pytest.mark.asyncio
async def test_list_workspace_files_uses_action_proxy_for_openclaw_without_runtime_api_key(
    monkeypatch,
):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    recorded: dict[str, object] = {}

    async def _fake_get_agent(**kwargs):
        assert kwargs["include_api_key"] is True
        return {
            "basic": {"agent_id": "ar-openclaw", "name": "demo-openclaw"},
            "deployment": {"framework": "openclaw"},
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
            },
        }

    def _fake_action(action, params=None):
        recorded["action"] = action
        recorded["params"] = params
        return {
            "root": "workspace",
            "path": "docs",
            "entries": [{"name": "guide.md", "path": "docs/guide.md", "type": "file"}],
        }

    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_action", _fake_action)
    monkeypatch.setattr(
        client,
        "_workspace_runtime_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime direct path must not be used")),
    )

    payload = await client.list_workspace_files(agent_id="ar-openclaw", path="docs", recursive=True)

    assert payload["path"] == "docs"
    assert payload["entries"][0]["path"] == "docs/guide.md"
    assert recorded == {
        "action": "ListWorkspaceFiles",
        "params": {
            "AgentId": "ar-openclaw",
            "Name": "demo-openclaw",
            "Path": "docs",
            "Recursive": True,
        },
    }


@pytest.mark.asyncio
async def test_list_workspace_files_uses_action_proxy_for_openclaw_even_with_api_key(
    monkeypatch,
):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    recorded: dict[str, object] = {}

    async def _fake_get_agent(**kwargs):
        assert kwargs["include_api_key"] is True
        return {
            "basic": {"agent_id": "ar-openclaw", "name": "demo-openclaw"},
            "deployment": {"framework": "openclaw"},
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
                "api_key": "ak-openclaw",
            },
        }

    def _fake_action(action, params=None):
        recorded["action"] = action
        recorded["params"] = params
        return {
            "root": "workspace",
            "path": ".",
            "entries": [{"name": "guide.md", "path": "guide.md", "type": "file"}],
        }

    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_action", _fake_action)
    monkeypatch.setattr(
        client,
        "_workspace_runtime_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime direct path must not be used")),
    )

    payload = await client.list_workspace_files(agent_id="ar-openclaw")

    assert payload["entries"][0]["path"] == "guide.md"
    assert recorded == {
        "action": "ListWorkspaceFiles",
        "params": {
            "AgentId": "ar-openclaw",
            "Name": "demo-openclaw",
            "Path": ".",
            "Recursive": False,
        },
    }


@pytest.mark.asyncio
async def test_workspace_file_data_plane_uses_action_proxy_for_openclaw_without_runtime_api_key(
    monkeypatch,
    tmp_path: Path,
):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    recorded: list[dict[str, object]] = []

    async def _fake_get_agent(**kwargs):
        assert kwargs["include_api_key"] is True
        return {
            "basic": {"agent_id": "ar-openclaw", "name": "demo-openclaw"},
            "deployment": {"framework": "openclaw"},
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
            },
        }

    def _fake_action(action, params=None):
        recorded.append({"action": action, "params": params})
        if action == "DeleteWorkspaceFile":
            return {"deleted": True}
        raise AssertionError(f"unexpected json action {action}")

    def _fake_action_raw_request(method, action, *, params=None, data=None, files=None, accept="application/json"):
        recorded.append(
            {
                "method": method,
                "action": action,
                "params": params,
                "data": data,
                "files": files,
                "accept": accept,
            }
        )
        if action == "AddWorkspaceFile":
            return _FakeRuntimeResponse(
                json_payload={
                    "Entry": {
                        "Name": "report.txt",
                        "Path": "reports/report.txt",
                        "Type": "file",
                        "SizeBytes": 15,
                    }
                }
            )
        if action == "GetWorkspaceFileContent":
            return _FakeRuntimeResponse(
                content=b"workspace hello",
                headers={"content-type": "text/plain"},
            )
        raise AssertionError(f"unexpected raw action {action}")

    local_file = tmp_path / "report.txt"
    local_file.write_text("workspace hello", encoding="utf-8")

    monkeypatch.setattr(client, "get_agent", _fake_get_agent)
    monkeypatch.setattr(client, "_action", _fake_action)
    monkeypatch.setattr(client, "_action_raw_request", _fake_action_raw_request)
    monkeypatch.setattr(
        client,
        "_workspace_runtime_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runtime direct path must not be used")),
    )

    upload_payload = await client.upload_workspace_file(
        agent_id="ar-openclaw",
        remote_path="reports/report.txt",
        local_path=local_file,
    )
    download_payload = await client.download_workspace_file(
        agent_id="ar-openclaw",
        remote_path="reports/report.txt",
    )
    delete_payload = await client.delete_workspace_file(
        agent_id="ar-openclaw",
        remote_path="reports/report.txt",
    )

    assert upload_payload["entry"]["path"] == "reports/report.txt"
    assert download_payload == b"workspace hello"
    assert delete_payload["deleted"] is True
    assert delete_payload["transport_mode"] == "action_proxy"
    assert recorded[0]["action"] == "AddWorkspaceFile"
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["data"] == {
        "AgentId": "ar-openclaw",
        "Name": "demo-openclaw",
        "Path": "reports/report.txt",
    }
    assert recorded[0]["files"] is not None
    assert recorded[1] == {
        "method": "GET",
        "action": "GetWorkspaceFileContent",
        "params": {
            "AgentId": "ar-openclaw",
            "Name": "demo-openclaw",
            "FilePath": "reports/report.txt",
        },
        "data": None,
        "files": None,
        "accept": "application/octet-stream",
    }
    assert recorded[2] == {
        "action": "DeleteWorkspaceFile",
        "params": {
            "AgentId": "ar-openclaw",
            "Name": "demo-openclaw",
            "Path": "reports/report.txt",
        },
    }
