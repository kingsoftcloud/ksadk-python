from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from ksadk.tunnel import LocalServer, TunnelConnector
from ksadk.tunnel.connector import _local_request_url


def test_public_package_import_is_lazy() -> None:
    script = """
import sys
import ksadk.tunnel as tunnel
assert set(tunnel.__all__) == {
    'LocalServer', 'TunnelConnector', 'ServerDescriptor', 'TunnelRegistry',
    'TunnelToolset', 'LangGraphTunnelTools', 'mount_tunnel', 'get_registry'
}
assert 'ksadk.tunnel.connector' not in sys.modules
assert 'ksadk.tunnel.toolset' not in sys.modules
assert 'ksadk.tunnel.langgraph' not in sys.modules
assert not any(name == 'google.adk' or name.startswith('google.adk.') for name in sys.modules)
import ksadk.tunnel.config
assert not any(name == 'google.adk' or name.startswith('google.adk.') for name in sys.modules)
from ksadk.tunnel import TunnelRegistry
assert TunnelRegistry().self_url is None
from ksadk.tunnel import LangGraphTunnelTools
assert LangGraphTunnelTools.__name__ == 'LangGraphTunnelTools'
assert 'langchain_mcp_adapters' not in sys.modules
assert not any(name == 'google.adk' or name.startswith('google.adk.') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_descriptor_contains_no_local_connection_material() -> None:
    server = LocalServer(
        name="docs",
        address="http://local.example/mcp?address_secret=hidden",
        tool_filter=["search"],
        headers={"Authorization": "Bearer local-header-secret"},
        query={"api_key": "local-query-secret"},
    )

    assert server.descriptor() == {
        "name": "docs",
        "protocol": "mcp",
        "tool_filter": ["search"],
    }
    serialized = json.dumps(server.descriptor())
    assert "secret" not in serialized
    assert "local-header-secret" not in repr(server)
    assert "local-query-secret" not in repr(server)
    assert "address_secret" not in repr(server)


@pytest.mark.parametrize(
    ("cloud_url", "expected_scheme"),
    [
        ("http://cloud.example/base", "ws"),
        ("https://cloud.example/base", "wss"),
        ("ws://cloud.example/base", "ws"),
        ("wss://cloud.example/base", "wss"),
    ],
)
def test_websocket_url_converts_scheme_and_encodes_token(
    cloud_url: str, expected_scheme: str
) -> None:
    token = "token with +/&=?%"
    connector = TunnelConnector(
        f"{cloud_url}?gateway=existing",
        "agent",
        [LocalServer("docs", "http://local.example/mcp")],
        token=token,
        extra_headers={"authorization": "Bearer fixture-gateway-token"},
    )

    parsed = urlsplit(connector._ws_url())
    assert parsed.scheme == expected_scheme
    assert parsed.path == "/base/tunnel/connect"
    assert parse_qs(parsed.query) == {"gateway": ["existing"], "token": [token]}


def test_websocket_url_is_complete_tunnel_endpoint_without_base_path() -> None:
    connector = TunnelConnector(
        "https://cloud.example/",
        "agent",
        [LocalServer("docs", "http://local.example/mcp")],
        token="fake-token",
    )

    assert connector._ws_url() == "wss://cloud.example/tunnel/connect"


def test_token_defaults_to_handshake_header_not_url_or_register_body() -> None:
    token = "fake-token"
    connector = TunnelConnector(
        "https://cloud.example/base",
        "agent",
        [LocalServer("docs", "http://local.example/mcp")],
        token=token,
    )

    assert "token" not in parse_qs(urlsplit(connector._ws_url()).query)
    assert connector._handshake_headers() == {"Authorization": f"Bearer {token}"}
    register = connector._register_message()
    assert "token" not in register
    assert token not in json.dumps(register)


def test_websockets_header_argument_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    import ksadk.tunnel.connector as connector_module

    def modern(uri, *, additional_headers=None):
        return uri, additional_headers

    def legacy(uri, *, extra_headers=None):
        return uri, extra_headers

    monkeypatch.setattr(connector_module.websockets, "connect", modern)
    assert TunnelConnector._header_kwargs({"X-Gateway": "one"}) == {
        "additional_headers": {"X-Gateway": "one"}
    }
    monkeypatch.setattr(connector_module.websockets, "connect", legacy)
    assert TunnelConnector._header_kwargs({"X-Gateway": "two"}) == {
        "extra_headers": {"X-Gateway": "two"}
    }


@pytest.mark.asyncio
async def test_registration_ack_has_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import ksadk.tunnel.connector as connector_module

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, raw):
            self.messages.append(json.loads(raw))

        async def recv(self):
            await asyncio.Event().wait()

    class FakeConnect:
        async def __aenter__(self):
            return websocket

        async def __aexit__(self, *args):
            return None

    websocket = FakeWebSocket()

    def connect(uri, *, additional_headers=None):
        return FakeConnect()

    monkeypatch.setattr(connector_module.websockets, "connect", connect)
    monkeypatch.setattr(connector_module, "_REGISTER_ACK_TIMEOUT_SECONDS", 0.01)
    connector = TunnelConnector(
        "https://cloud.example",
        "agent",
        [LocalServer("docs", "http://local.example/mcp")],
        token="fixture-token",
    )

    with pytest.raises(TimeoutError):
        await connector.start()
    assert websocket.messages[0]["type"] == "register"


def test_local_url_preserves_forwarded_query_and_local_secrets_win() -> None:
    server = LocalServer(
        "docs",
        "http://local.example/mcp?base=one&api_key=old",
        query={"api_key": "new secret", "signature": "+/&"},
    )
    url = _local_request_url(
        server,
        {"path": "/sessions", "query": "q=hello+world&api_key=forwarded"},
    )
    parsed = urlsplit(url)
    assert parsed.path == "/mcp/sessions"
    assert parse_qs(parsed.query) == {
        "base": ["one"],
        "q": ["hello world"],
        "api_key": ["new secret"],
        "signature": ["+/&"],
    }


@pytest.mark.asyncio
async def test_connector_runs_requests_concurrently_and_reaps_cancelled_tasks() -> None:
    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, body: str, started: asyncio.Event, release: asyncio.Event) -> None:
            self.body = body
            self.started = started
            self.release = release

        async def aiter_bytes(self):
            if self.body == "slow":
                self.started.set()
                await self.release.wait()
            yield self.body.encode()

    class FakeStream:
        def __init__(self, response: FakeResponse) -> None:
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            return None

    class FakeHttp:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def stream(self, *, content, **kwargs):
            return FakeStream(FakeResponse(content.decode(), self.started, self.release))

    class FakeWs:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, raw) -> None:
            self.messages.append(json.loads(raw))

    connector = TunnelConnector(
        "http://cloud.example",
        "agent",
        [LocalServer("docs", "http://local.example/mcp")],
    )
    websocket = FakeWs()
    http = FakeHttp()
    connector._start_request(websocket, http, {"id": "slow", "server": "docs", "body": "slow"})
    slow_task = connector._tasks["slow"]
    await asyncio.wait_for(http.started.wait(), timeout=1)
    connector._start_request(websocket, http, {"id": "fast", "server": "docs", "body": "fast"})

    for _ in range(20):
        if any(
            message.get("type") == "http_end" and message.get("id") == "fast"
            for message in websocket.messages
        ):
            break
        await asyncio.sleep(0)
    assert any(
        message.get("type") == "http_end" and message.get("id") == "fast"
        for message in websocket.messages
    )
    assert "slow" in connector._tasks

    connector._cancel_request("slow")
    with pytest.raises(asyncio.CancelledError):
        await slow_task
    connector._start_request(websocket, http, {"id": "cleanup", "server": "docs", "body": "slow"})
    cleanup_task = connector._tasks["cleanup"]
    await connector._cancel_all_tasks()
    assert slow_task.done()
    assert cleanup_task.done()
    assert connector._tasks == {}
