from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "deploy" / "openclaw" / "openclaw_runtime_proxy_app.py"


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("ksadk_openclaw_runtime_proxy_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, *, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}

    async def aread(self):
        return self._content

    async def aclose(self):
        return None

    async def aiter_raw(self):
        yield self._content


class _FakeAsyncClient:
    routes = {}
    send_calls = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def build_request(self, method, url, headers=None, content=None):
        return type(
            "Request",
            (),
            {
                "method": method.upper(),
                "url": url,
                "headers": headers or {},
                "content": content,
            },
        )()

    async def send(self, request, stream=False):
        self.__class__.send_calls.append(
            (request.method, request.url, request.headers, request.content, bool(stream))
        )
        try:
            return self.__class__.routes[(request.method, request.url)]
        except KeyError as exc:
            raise AssertionError(f"unexpected upstream request: {(request.method, request.url)}") from exc

    async def aclose(self):
        return None


def test_runtime_proxy_resolves_openclaw_tui_command_with_gateway_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_INTERNAL_PORT", "18080")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token")
    module = _load_runtime_module()

    assert module._resolve_terminal_command("tui", [], session_id="sess-1") == [
        "openclaw",
        "tui",
        "--url",
        "ws://127.0.0.1:18080/",
        "--token",
        "gateway-token",
        "--session",
        "sess-1",
    ]


def test_runtime_proxy_maps_whitelisted_tui_options_to_openclaw_cli(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_INTERNAL_PORT", "18080")
    module = _load_runtime_module()

    assert module._resolve_terminal_command(
        "tui",
        [],
        session_id="sess-1",
        options={
            "message": "你好",
            "thinking": "medium",
            "history_limit": 50,
            "timeout_ms": 30000,
            "deliver": True,
        },
    ) == [
        "openclaw",
        "tui",
        "--url",
        "ws://127.0.0.1:18080/",
        "--session",
        "sess-1",
        "--message",
        "你好",
        "--thinking",
        "medium",
        "--history-limit",
        "50",
        "--timeout-ms",
        "30000",
        "--deliver",
    ]


def test_runtime_proxy_resolves_exec_command_as_raw_argv():
    module = _load_runtime_module()

    assert module._resolve_terminal_command(
        "exec",
        ["openclaw", "channels", "login", "--channel", "openclaw-weixin"],
        session_id=None,
    ) == ["openclaw", "channels", "login", "--channel", "openclaw-weixin"]


@pytest.mark.parametrize("mode,argv", [("exec", []), ("exec", ["sh", "-lc", "id"]), ("tui", ["status"]), ("connect", [])])
def test_runtime_proxy_rejects_unsafe_terminal_modes_or_argv(mode, argv):
    module = _load_runtime_module()

    with pytest.raises(ValueError):
        module._resolve_terminal_command(mode, argv, session_id=None)


def test_runtime_proxy_routes_http_to_openclaw_gateway(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_PROXY_BASE_URL", "http://127.0.0.1:18080")
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "POST",
            "http://127.0.0.1:18080/v1/responses?stream=true",
        ): _FakeResponse(status_code=202, content=b'{"ok":true}', headers={"x-upstream": "gateway"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.post(
            "/v1/responses?stream=true",
            json={"input": "hi"},
            headers={"x-test": "openclaw"},
        )

    assert response.status_code == 202
    assert response.content == b'{"ok":true}'
    assert response.headers["x-upstream"] == "gateway"
    method, url, headers, content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:18080/v1/responses?stream=true"
    assert stream is True
    assert headers["x-test"] == "openclaw"
    assert b'"input":"hi"' in (content or b"")


def test_runtime_proxy_attaches_operator_scope_for_matching_gateway_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_PROXY_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token")
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:18080/v1/models",
        ): _FakeResponse(status_code=200, content=b'{"object":"list"}'),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get(
            "/v1/models",
            headers={
                "authorization": "Bearer gateway-token",
                "x-openclaw-scopes": "operator.read",
            },
        )

    assert response.status_code == 200
    _method, _url, headers, _content, _stream = _FakeAsyncClient.send_calls[0]
    assert headers["x-openclaw-scopes"] == "operator.write"


def test_runtime_proxy_does_not_attach_operator_scope_for_wrong_gateway_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_PROXY_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token")
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:18080/v1/models",
        ): _FakeResponse(status_code=403, content=b'{"ok":false}'),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/v1/models", headers={"authorization": "Bearer wrong-token"})

    assert response.status_code == 403
    _method, _url, headers, _content, _stream = _FakeAsyncClient.send_calls[0]
    assert "x-openclaw-scopes" not in {key.lower(): value for key, value in headers.items()}


def test_runtime_proxy_rejects_terminal_without_bearer_token_when_gateway_token_is_set(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gateway-token")
    module = _load_runtime_module()

    with TestClient(module.app) as client:
        with client.websocket_connect(
            "/_ksadk/terminal/ws",
            subprotocols=[module.TERMINAL_SUBPROTOCOL],
        ) as ws:
            ws.send_text('{"type":"start","mode":"tui","argv":[],"cols":80,"rows":24}')
            payload = ws.receive_json()

    assert payload["type"] == "error"
    assert "OPENCLAW_GATEWAY_TOKEN" in payload["message"]


def test_runtime_proxy_exposes_terminal_session_control_plane(monkeypatch):
    module = _load_runtime_module()
    monkeypatch.setattr(module, "_spawn_terminal_session", lambda session: None)

    with TestClient(module.app) as client:
        create_response = client.post(
            "/_ksadk/terminal/sessions",
            json={"mode": "tui", "cols": 100, "rows": 30, "session_id": "main"},
        )
        list_response = client.get("/_ksadk/terminal/sessions")

    assert create_response.status_code == 200
    created = create_response.json()["session"]
    assert created["terminal_session_id"].startswith("term-")
    assert created["mode"] == "tui"
    assert created["status"] == "running"
    assert created["cols"] == 100
    assert created["rows"] == 30
    assert created["session_id"] == "main"
    assert list_response.status_code == 200
    assert list_response.json()["sessions"][0]["terminal_session_id"] == created["terminal_session_id"]


def test_runtime_proxy_uses_unique_openclaw_session_keys_by_default(monkeypatch):
    module = _load_runtime_module()
    monkeypatch.setattr(module, "_spawn_terminal_session", lambda session: None)

    with TestClient(module.app) as client:
        first = client.post("/_ksadk/terminal/sessions", json={"mode": "tui"}).json()["session"]
        second = client.post("/_ksadk/terminal/sessions", json={"mode": "tui"}).json()["session"]

    assert first["terminal_session_id"].startswith("term-")
    assert second["terminal_session_id"].startswith("term-")
    assert first["terminal_session_id"] != second["terminal_session_id"]
    assert first["session_id"] == first["terminal_session_id"]
    assert second["session_id"] == second["terminal_session_id"]


def test_runtime_proxy_spawns_openclaw_tui_with_unique_session_keys(monkeypatch):
    module = _load_runtime_module()
    commands: list[list[str]] = []

    def fake_spawn(session):
        commands.append(
            module._resolve_terminal_command(
                session.mode,
                session.argv,
                session_id=session.session_id,
                options=session.options,
            )
        )
        session.status = "running"

    monkeypatch.setattr(module, "_spawn_terminal_session", fake_spawn)

    with TestClient(module.app) as client:
        first = client.post("/_ksadk/terminal/sessions", json={"mode": "tui"}).json()["session"]
        second = client.post("/_ksadk/terminal/sessions", json={"mode": "tui"}).json()["session"]

    assert commands[0][commands[0].index("--session") + 1] == first["terminal_session_id"]
    assert commands[1][commands[1].index("--session") + 1] == second["terminal_session_id"]
    assert commands[0] != commands[1]


def test_runtime_proxy_closes_terminal_session(monkeypatch):
    module = _load_runtime_module()
    closed: list[str] = []
    monkeypatch.setattr(module, "_spawn_terminal_session", lambda session: None)
    monkeypatch.setattr(module, "_terminate_terminal_session", lambda session: closed.append(session.id))

    with TestClient(module.app) as client:
        terminal_session_id = client.post(
            "/_ksadk/terminal/sessions",
            json={"mode": "tui"},
        ).json()["session"]["terminal_session_id"]
        close_response = client.delete(f"/_ksadk/terminal/sessions/{terminal_session_id}")
        list_response = client.get("/_ksadk/terminal/sessions")

    assert close_response.status_code == 200
    assert close_response.json() == {"closed": True, "terminal_session_id": terminal_session_id}
    assert closed == [terminal_session_id]
    assert list_response.json()["sessions"] == []
