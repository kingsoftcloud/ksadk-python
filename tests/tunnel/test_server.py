from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ksadk.tunnel.registry import ServerDescriptor, TunnelRegistry
from ksadk.tunnel.server import (
    ConnectorConnection,
    TunnelConnectionError,
    TunnelStreamError,
    mount_tunnel,
)

from .conftest import add_connection, request_frames

TUNNEL_TOKEN = "test-tunnel-token"


def app_and_registry(**kwargs):
    app = FastAPI()
    registry = TunnelRegistry()
    mount_tunnel(app, token=TUNNEL_TOKEN, registry=registry, **kwargs)
    return app, registry


def register_payload(**overrides):
    payload = {
        "type": "register",
        "agent": "agent",
        "token": TUNNEL_TOKEN,
        "servers": [{"name": "docs", "protocol": "mcp", "tool_filter": None}],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_mount_rejects_unauthenticated_configuration() -> None:
    with pytest.raises(ValueError):
        mount_tunnel(FastAPI())
    with pytest.raises(ValueError):
        mount_tunnel(FastAPI(), token="")

    app = FastAPI()
    mount_tunnel(app, auth=lambda presented, agent: True)
    assert app.routes


def test_websocket_auth_malformed_registration_and_disconnect_cleanup() -> None:
    app, registry = app_and_registry(allowed_agents=["agent"])
    with TestClient(app) as client:
        with client.websocket_connect("/tunnel/connect") as websocket:
            websocket.send_text(register_payload(token="wrong"))
            assert websocket.receive_json()["ok"] is False
            assert websocket.receive()["code"] == 4403
        assert registry.list_servers("agent") == []

        with client.websocket_connect("/tunnel/connect") as websocket:
            websocket.send_text("not-json")
            assert websocket.receive_json()["ok"] is False
            assert websocket.receive()["code"] == 4400

        with client.websocket_connect("/tunnel/connect") as websocket:
            websocket.send_text(register_payload(agent="other"))
            assert websocket.receive_json()["ok"] is False
            assert websocket.receive()["code"] == 4403

        with client.websocket_connect("/tunnel/connect") as websocket:
            websocket.send_text(register_payload(token=None))
            # The query token is the preferred gateway-compatible credential path.
            assert websocket.receive_json()["ok"] is False
            assert websocket.receive()["code"] == 4403

        with client.websocket_connect(f"/tunnel/connect?token={TUNNEL_TOKEN}") as websocket:
            websocket.send_text(register_payload(token=None))
            assert websocket.receive_json()["ok"] is True
            assert [server.name for server in registry.list_servers("agent")] == ["docs"]
        assert registry.list_servers("agent") == []


def test_websocket_rejects_invalid_server_sets_without_partial_registration() -> None:
    app, registry = app_and_registry()
    with TestClient(app) as client:
        invalid_sets = [
            [],
            [{"name": "same"}, {"name": "same"}],
            [{"name": "bad", "protocol": "stdio"}],
            [{"name": " ", "protocol": "mcp"}],
        ]
        for servers in invalid_sets:
            with client.websocket_connect("/tunnel/connect") as websocket:
                websocket.send_text(register_payload(servers=servers))
                assert websocket.receive_json()["ok"] is False
                assert websocket.receive()["code"] == 4400
            assert registry.list_servers("agent") == []


def test_proxy_auth_header_stripping_path_query_and_streaming() -> None:
    app, registry = app_and_registry()
    holder = {}

    def respond(message):
        holder["connection"].dispatch(
            {
                "type": "http_response",
                "id": message["id"],
                "status": 200,
                "headers": {"Content-Type": "text/plain", "Content-Length": "999"},
            }
        )
        for data in ["first:", "second"]:
            holder["connection"].dispatch({"type": "http_chunk", "id": message["id"], "data": data})
        holder["connection"].dispatch({"type": "http_end", "id": message["id"]})

    connection, websocket = add_connection(registry, responder=respond)
    holder["connection"] = connection
    with TestClient(app) as client:
        unauthenticated = client.post("/tunnel/mcp/agent/docs")
        assert unauthenticated.status_code == 401
        assert request_frames(websocket) == []

        response = client.post(
            "/tunnel/mcp/agent/docs/sessions",
            params={"q": "hello world"},
            headers={
                "Authorization": f"Bearer {registry.proxy_token}",
                "Proxy-Authorization": "must-not-leak",
                "Mcp-Session-Id": "session-one",
                "X-Test": "preserved",
            },
            content='{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.content == b"first:second"
    assert "content-length" not in response.headers
    frame = request_frames(websocket)[0]
    assert frame["path"] == "/sessions"
    assert frame["query"] == "q=hello+world"
    assert frame["headers"]["mcp-session-id"] == "session-one"
    assert frame["headers"]["x-test"] == "preserved"
    assert "authorization" not in frame["headers"]
    assert "proxy-authorization" not in frame["headers"]
    assert registry.proxy_token not in json.dumps(frame)


def test_servers_requires_agent_and_tunnel_auth() -> None:
    app, registry = app_and_registry(allowed_agents=["agent"])
    add_connection(registry)
    with TestClient(app) as client:
        assert client.get("/tunnel/servers").status_code == 400
        assert client.get("/tunnel/servers?agent=agent").status_code == 403
        assert (
            client.get(
                "/tunnel/servers?agent=agent",
                headers={"Authorization": f"Bearer {registry.proxy_token}"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/tunnel/servers?agent=other",
                headers={"Authorization": f"Bearer {TUNNEL_TOKEN}"},
            ).status_code
            == 403
        )
        response = client.get(
            "/tunnel/servers?agent=agent",
            headers={"Authorization": f"Bearer {TUNNEL_TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "agent": "agent",
        "servers": [{"name": "docs", "protocol": "mcp", "tool_filter": None}],
    }
    assert registry.proxy_token not in response.text


def test_custom_auth_applies_to_registration_and_server_listing() -> None:
    calls = []

    def authenticate(presented, agent):
        calls.append((presented, agent))
        return presented == "custom" and agent == "agent"

    app = FastAPI()
    registry = TunnelRegistry()
    mount_tunnel(app, auth=authenticate, registry=registry)
    add_connection(registry)
    with TestClient(app) as client:
        assert client.get("/tunnel/servers?agent=agent&token=wrong").status_code == 403
        assert client.get("/tunnel/servers?agent=agent&token=custom").status_code == 200
    assert calls == [("wrong", "agent"), ("custom", "agent")]


def test_async_custom_auth_is_awaited_and_fails_closed() -> None:
    async def authenticate(presented, agent):
        await asyncio.sleep(0)
        return False

    app = FastAPI()
    registry = TunnelRegistry()
    mount_tunnel(app, auth=authenticate, registry=registry)
    with TestClient(app) as client:
        with client.websocket_connect("/tunnel/connect") as websocket:
            websocket.send_text(register_payload())
            assert websocket.receive_json()["ok"] is False
            assert websocket.receive()["code"] == 4403
        response = client.get("/tunnel/servers?agent=agent&token=custom")

    assert response.status_code == 403
    assert registry.list_servers("agent") == []


def test_head_timeout_cleans_pending_and_sends_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ksadk.tunnel.server._HEAD_TIMEOUT_SECONDS", 0.01)
    app, registry = app_and_registry()
    _, websocket = add_connection(registry)
    with TestClient(app) as client:
        response = client.post(
            "/tunnel/mcp/agent/docs",
            headers={"Authorization": f"Bearer {registry.proxy_token}"},
        )
    assert response.status_code == 504
    assert [message["type"] for message in websocket.messages] == [
        "http_request",
        "http_cancel",
    ]
    request_id = websocket.messages[0]["id"]
    assert websocket.messages[1]["id"] == request_id
    assert registry.find_connection("agent", "docs").pending == {}


def test_disconnect_before_head_fails_request() -> None:
    app, registry = app_and_registry()
    holder = {}

    def disconnect(message):
        holder["connection"].fail_all()

    connection, _ = add_connection(registry, responder=disconnect)
    holder["connection"] = connection
    with TestClient(app) as client:
        response = client.post(
            "/tunnel/mcp/agent/docs",
            headers={"Authorization": f"Bearer {registry.proxy_token}"},
        )
    assert response.status_code == 502
    assert connection.pending == {}


def test_stream_error_is_not_converted_to_normal_eof() -> None:
    app, registry = app_and_registry()
    holder = {}

    def fail_midstream(message):
        connection = holder["connection"]
        connection.dispatch(
            {
                "type": "http_response",
                "id": message["id"],
                "status": 200,
                "headers": {},
            }
        )
        connection.dispatch({"type": "http_chunk", "id": message["id"], "data": "partial"})
        connection.dispatch({"type": "http_error", "id": message["id"]})

    connection, _ = add_connection(registry, responder=fail_midstream)
    holder["connection"] = connection
    with TestClient(app) as client:
        with pytest.raises(TunnelStreamError):
            with client.stream(
                "POST",
                "/tunnel/mcp/agent/docs",
                headers={"Authorization": f"Bearer {registry.proxy_token}"},
            ) as response:
                assert response.status_code == 200
                list(response.iter_bytes())
    assert connection.pending == {}


@pytest.mark.asyncio
async def test_http_disconnect_cancels_stream_and_notifies_connector() -> None:
    app, registry = app_and_registry()
    holder = {}

    def send_head(message):
        holder["connection"].dispatch(
            {
                "type": "http_response",
                "id": message["id"],
                "status": 200,
                "headers": {},
            }
        )

    connection, websocket = add_connection(registry, responder=send_head)
    holder["connection"] = connection
    incoming = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    response_started = asyncio.Event()
    sent = []

    async def receive():
        return await incoming.get()

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start":
            response_started.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/tunnel/mcp/agent/docs",
        "raw_path": b"/tunnel/mcp/agent/docs",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {registry.proxy_token}".encode()),
        ],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    app_task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(response_started.wait(), timeout=1)
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(app_task, timeout=1)

    assert [message["type"] for message in websocket.messages] == [
        "http_request",
        "http_cancel",
    ]
    assert connection.pending == {}
    assert sent[0]["type"] == "http.response.start"


@pytest.mark.asyncio
async def test_cancel_during_send_also_notifies_connector() -> None:
    class BlockingWebSocket:
        def __init__(self) -> None:
            self.messages = []
            self.request_sent = asyncio.Event()

        async def send_text(self, raw):
            message = json.loads(raw)
            self.messages.append(message)
            if message["type"] == "http_request":
                self.request_sent.set()
                await asyncio.Event().wait()

    registry = TunnelRegistry()
    websocket = BlockingWebSocket()
    connection = ConnectorConnection(
        connector_id="connector",
        websocket=websocket,
        agent_name="agent",
        servers=[ServerDescriptor(name="docs")],
    )
    registry.add_connection(connection)
    request_task = asyncio.create_task(connection.request("docs", {}))
    await asyncio.wait_for(websocket.request_sent.wait(), timeout=1)
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request_task, timeout=1)

    assert [message["type"] for message in websocket.messages] == [
        "http_request",
        "http_cancel",
    ]
    assert connection.pending == {}


@pytest.mark.asyncio
async def test_cancel_send_itself_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ksadk.tunnel.server._CANCEL_TIMEOUT_SECONDS", 0.01)

    class BlockingCancelWebSocket:
        def __init__(self) -> None:
            self.messages = []

        async def send_text(self, raw):
            self.messages.append(json.loads(raw))
            if self.messages[-1]["type"] == "http_cancel":
                await asyncio.Event().wait()

    registry = TunnelRegistry()
    websocket = BlockingCancelWebSocket()
    connection = ConnectorConnection(
        connector_id="connector",
        websocket=websocket,
        agent_name="agent",
        servers=[ServerDescriptor(name="docs")],
    )
    registry.add_connection(connection)
    queue = await connection.request("docs", {})

    await asyncio.wait_for(connection.cancel(queue.request_id), timeout=0.2)

    assert [message["type"] for message in websocket.messages] == [
        "http_request",
        "http_cancel",
    ]
    assert connection.pending == {}


@pytest.mark.asyncio
async def test_cancel_disconnect_and_late_frames_are_scoped_to_request_id() -> None:
    registry = TunnelRegistry()
    connection, websocket = add_connection(registry)
    queue = await connection.request("docs", {})
    request_id = queue.request_id
    connection.dispatch(
        {
            "type": "http_response",
            "id": request_id,
            "status": 200,
            "headers": {},
        }
    )
    connection.dispatch({"type": "http_chunk", "id": request_id, "data": "ok"})
    connection.dispatch({"type": "http_end", "id": request_id})
    connection.dispatch({"type": "http_error", "id": request_id})
    connection.dispatch({"type": "http_chunk", "id": request_id, "data": "late"})

    assert queue.get_nowait()[0] == "head"
    assert queue.get_nowait() == ("chunk", "ok")
    assert queue.get_nowait() == ("end",)
    assert queue.empty()
    assert connection.pending == {}

    cancelled = await connection.request("docs", {})
    await connection.cancel(cancelled.request_id)
    assert websocket.messages[-1] == {"type": "http_cancel", "id": cancelled.request_id}
    connection.dispatch({"type": "http_chunk", "id": cancelled.request_id, "data": "late"})
    assert cancelled.empty()

    disconnected = await connection.request("docs", {})
    connection.fail_all()
    assert disconnected.get_nowait() == ("error", "connector disconnected")
    with pytest.raises(TunnelConnectionError):
        await connection.request("docs", {})
