from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from ksadk.studio import dsh_application
from ksadk.studio.errors import StudioError


@pytest.fixture
def application(monkeypatch):
    app = FastAPI()

    @app.exception_handler(StudioError)
    async def error(_request, exc):
        return JSONResponse(exc.as_dict(), status_code=exc.status_code)

    capabilities = SimpleNamespace(application_lease=AsyncMock(return_value=SimpleNamespace(endpoint="http://127.0.0.1:43123/mcp")))
    dsh_application.register_dsh_application(app, SimpleNamespace(dsh_capabilities=capabilities), session_secret="test-session", security_enabled=True)
    observed = []

    def upstream(request):
        observed.append((str(request.url), request.headers.get("origin"), request.read()))
        return httpx.Response(200, stream=httpx.ByteStream(b"event: result\ndata: ok\n\n"), headers={"content-type": "text/event-stream"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(dsh_application.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(upstream), **kw))
    with TestClient(app) as client:
        yield client, capabilities, observed


def test_core_routes_require_the_studio_session(application):
    client, capabilities, _ = application
    assert client.get("/api/session/modelCatalog").status_code == 401
    capabilities.application_lease.assert_not_awaited()


def test_core_mutations_reject_cross_port_and_missing_origin(application):
    client, capabilities, _ = application
    client.cookies.set("agentkit_studio_session", "test-session")
    for headers in ({}, {"Origin": "http://testserver:9999"}, {"Origin": "https://example.com"}):
        assert client.post("/api/plugin/settings", headers=headers).status_code == 403
    capabilities.application_lease.assert_not_awaited()


def test_studio_api_misses_never_fall_through_to_core(application):
    client, capabilities, _ = application
    client.cookies.set("agentkit_studio_session", "test-session")
    assert client.get("/api/v1/missing").status_code == 404
    capabilities.application_lease.assert_not_awaited()


def test_core_proxy_preserves_stream_body_and_rewrites_only_transport_origin(application):
    client, _, observed = application
    client.cookies.set("agentkit_studio_session", "test-session")
    response = client.post("/studio-core/api/example?q=test", content=b'{"args":{}}', headers={"Origin": "http://testserver"})
    assert response.content == b"event: result\ndata: ok\n\n"
    assert response.headers["content-type"] == "text/event-stream"
    assert observed == [("http://127.0.0.1:43123/api/example?q=test", "http://127.0.0.1:43123", b'{"args":{}}')]


def test_websocket_checks_authentication_before_connecting(application):
    from starlette.websockets import WebSocketDisconnect

    client, capabilities, _ = application
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/api/channel", headers={"Origin": "http://testserver"}):
            pass
    assert error.value.code == 1008
    capabilities.application_lease.assert_not_awaited()


@pytest.mark.parametrize("path", ["/chat?agentId=demo-agent", "/chat/"])
def test_retired_chat_does_not_start_core(application, path):
    client, capabilities, observed = application
    client.cookies.set("agentkit_studio_session", "test-session")
    assert client.get(path).status_code == 404
    capabilities.application_lease.assert_not_awaited()
    assert observed == []
