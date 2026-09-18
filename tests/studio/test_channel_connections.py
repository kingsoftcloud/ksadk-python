from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ksadk.connector import InvokeRequest
from ksadk.studio.api import create_studio_app
from ksadk.studio.channel_connections import ChannelAgentConnectionInput, ChannelConnectionSettings
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


def test_channel_settings_require_tls_or_explicit_loopback():
    for url in (
        "http://remote.example.com",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
    ):
        with pytest.raises(ValidationError):
            ChannelConnectionSettings(serverUrl=url)
    assert ChannelConnectionSettings(serverUrl="http://127.0.0.1:8082/").serverUrl.endswith(":8082")


@pytest.mark.asyncio
async def test_channel_config_is_private_and_agent_must_exist(tmp_path):
    studio = StudioService(tmp_path)
    connection = studio.channel_connections
    try:
        status = await connection.configure(
            ChannelConnectionSettings(
                serverUrl="https://channel.example.test",
                accountId="test-tenant",
                apiToken="fixture-secret",
            )
        )
        assert "fixture-secret" not in json.dumps(status)
        assert status["hasApiToken"]
        assert studio.configuration.path.stat().st_mode & 0o077 == 0
        with pytest.raises(StudioError):
            await connection.configure_agent(
                "missing", ChannelAgentConnectionInput(enabled=True, token="fixture-agent-token")
            )
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_inbound_invocation_preserves_chat_sessions_and_serializes_turns(
    tmp_path, monkeypatch
):
    studio = StudioService(tmp_path)
    connection = studio.channel_connections
    build = SimpleNamespace(id="build-test")
    monkeypatch.setattr(studio, "ensure_current_build", AsyncMock(return_value=build))
    calls = []
    active = set()

    async def run(build_id, message, session_id):
        assert session_id not in active
        active.add(session_id)
        await asyncio.sleep(0.01)
        calls.append((build_id, message, session_id))
        active.remove(session_id)
        return SimpleNamespace(status="completed", output=message)

    monkeypatch.setattr(studio, "run_build", run)
    settings = ChannelConnectionSettings(accountId="tenant", workspaceId="workspace")
    try:
        results = await asyncio.gather(
            *[
                connection._invoke(
                    "agent",
                    settings,
                    InvokeRequest(
                        task_id=str(i), session_id="chat-a" if i < 2 else "chat-b", message=str(i)
                    ),
                )
                for i in range(3)
            ]
        )
        assert results == ["0", "1", "2"]
        by_message = {message: session for _, message, session in calls}
        assert by_message["0"] == by_message["1"] != by_message["2"]
    finally:
        await studio.aclose()


def test_channel_connection_api_requires_local_session_and_csrf_and_redacts(tmp_path):
    studio = StudioService(tmp_path)
    app = create_studio_app(
        tmp_path, service=studio, session_token="local-test", csrf_token="csrf-test"
    )
    # No lifespan is necessary for the configuration API; it never awaits DSH.
    with TestClient(app) as client:
        assert client.get("/api/v1/channels/connection").status_code == 401
        headers = {"X-AgentKit-Session": "local-test"}
        assert (
            client.put("/api/v1/channels/connection", headers=headers, json={}).status_code == 403
        )
        assert (
            client.post("/agentengine/api/v1/CreateChannel", headers=headers, json={}).status_code
            == 403
        )
        headers["X-CSRF-Token"] = "csrf-test"
        response = client.put(
            "/api/v1/channels/connection",
            headers=headers,
            json={
                "serverUrl": "https://channel.example.test",
                "accountId": "test-tenant",
                "apiToken": "fixture-secret",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["hasApiToken"] is True
        assert "fixture-secret" not in response.text
        assert (
            "fixture-secret" not in client.get("/api/v1/channels/connection", headers=headers).text
        )


@pytest.mark.asyncio
async def test_unconfigured_channel_never_calls_local_server(tmp_path):
    studio = StudioService(tmp_path)
    studio.configuration.inherited.pop("AGENTENGINE_CHANNEL_URL", None)
    try:
        with pytest.raises(StudioError, match="请先配置渠道服务"):
            await studio.channel_connections.proxy("ListChannels", {})
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_startup_restore_and_other_agent_edits_preserve_active_connector(
    tmp_path, monkeypatch
):
    from dataclasses import dataclass

    from ksadk.studio import channel_connections

    @dataclass
    class Status:
        state: str = "connected"
        connected: bool = True
        active_tasks: int = 0
        last_error: str | None = None

    class Connection:
        def __init__(self):
            self.status = Status()
            self.stopped = asyncio.Event()

        async def start(self):
            await self.stopped.wait()

        async def stop(self):
            self.stopped.set()

    monkeypatch.setattr(
        channel_connections, "create_channel_connector", lambda **kwargs: Connection()
    )
    studio = StudioService(tmp_path)
    monkeypatch.setattr(studio.drafts, "get", lambda agent_id: object())
    connections = studio.channel_connections
    try:
        await connections.configure(
            ChannelConnectionSettings(serverUrl="https://channel.example.test", accountId="test")
        )
        await connections.configure_agent("first", ChannelAgentConnectionInput(token="first-token"))
        first = connections._connections["first"]
        # The first inbound run starts Studio. Its background restore must not
        # close the connection carrying that run.
        connections.start_background()
        await connections._startup
        await connections.configure_agent(
            "second", ChannelAgentConnectionInput(token="second-token")
        )
        assert connections._connections["first"] is first
        assert not first.stopped.is_set()
        await connections.configure_agent("second", ChannelAgentConnectionInput(enabled=False))
        assert not first.stopped.is_set()
        await connections.configure_agent(
            "first", ChannelAgentConnectionInput(token="rotated-token")
        )
        assert first.stopped.is_set()
        assert connections._connections["first"] is not first
    finally:
        await studio.aclose()
