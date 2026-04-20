from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.api.client import AgentEngineClient


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
            raise AssertionError("json() called without a JSON payload")
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
    assert delete_payload == {"deleted": True}
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
