from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import threading
import time

import pytest
from starlette.testclient import TestClient


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "hermes"
    / "runtime"
    / "app.py"
)


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("ksadk_hermes_runtime_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, content: bytes = b"", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {"content-type": "application/json"}

    async def aread(self) -> bytes:
        return self._content

    async def aclose(self) -> None:
        return None


class _FakeStreamingResponse:
    def __init__(self, *, status_code: int = 200, chunks: list[bytes] | None = None, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}
        self._chunks = chunks or []
        self.closed = False

    async def aread(self) -> bytes:
        raise AssertionError("streaming upstream should not be fully buffered")

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FakeRequest:
    def __init__(self, method: str, url: str, headers: dict[str, str] | None, content: bytes | None):
        self.method = method
        self.url = url
        self.headers = headers
        self.content = content


class _FakeAsyncClient:
    routes: dict[tuple[str, str], _FakeResponse] = {}
    send_calls: list[tuple[str, str, dict[str, str] | None, bytes | None, bool]] = []
    request_calls: list[tuple[str, str, dict[str, str] | None, bytes | None]] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aclose(self) -> None:
        return None

    async def request(self, method, url, headers=None, content=None):
        self.__class__.request_calls.append((method, url, headers, content))
        try:
            return self.__class__.routes[(method.upper(), url)]
        except KeyError as exc:
            raise AssertionError(f"unexpected upstream request: {(method, url)}") from exc

    def build_request(self, method, url, headers=None, content=None):
        return _FakeRequest(method.upper(), url, headers, content)

    async def send(self, request, stream=False):
        self.__class__.send_calls.append(
            (request.method, request.url, request.headers, request.content, bool(stream))
        )
        try:
            return self.__class__.routes[(request.method.upper(), request.url)]
        except KeyError as exc:
            raise AssertionError(f"unexpected upstream request: {(request.method, request.url)}") from exc


def test_proxy_api_routes_to_hermes_api_server(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "POST",
            "http://127.0.0.1:8642/v1/chat/completions?stream=true",
        ): _FakeResponse(status_code=202, content=b'{"ok":true}', headers={"x-upstream": "api"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.post(
            "/v1/chat/completions?stream=true",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"x-test": "api"},
        )

    assert response.status_code == 202
    assert response.content == b'{"ok":true}'
    assert response.headers["x-upstream"] == "api"
    method, url, headers, content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8642/v1/chat/completions?stream=true"
    assert stream is True
    assert headers is not None and headers["x-test"] == "api"
    assert b'"content":"hi"' in (content or b"")


def test_proxy_dashboard_routes_root_to_hermes_dashboard(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:9119/",
        ): _FakeResponse(status_code=200, content=b"<html>dashboard</html>", headers={"content-type": "text/html"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text == "<html>dashboard</html>"
    method, url, _headers, _content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:9119/"
    assert stream is True


def test_proxy_api_preserves_sse_streaming(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "POST",
            "http://127.0.0.1:8642/v1/chat/completions?stream=true",
        ): _FakeStreamingResponse(
            status_code=200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
            chunks=[
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
                b"data: [DONE]\n\n",
            ],
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions?stream=true",
            json={"messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert body == b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
    method, url, _headers, _content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8642/v1/chat/completions?stream=true"
    assert stream is True


def test_health_checks_api_and_dashboard_upstreams(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.request_calls = []
    _FakeAsyncClient.routes = {
        ("GET", "http://127.0.0.1:8642/health"): _FakeResponse(content=b'{"ok":true}'),
        ("GET", "http://127.0.0.1:9119/"): _FakeResponse(content=b"<html>ok</html>", headers={"content-type": "text/html"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert ("GET", "http://127.0.0.1:8642/health", None, None) in _FakeAsyncClient.request_calls
    assert ("GET", "http://127.0.0.1:9119/", None, None) in _FakeAsyncClient.request_calls


def test_health_returns_503_when_dashboard_check_fails(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.request_calls = []
    _FakeAsyncClient.routes = {
        ("GET", "http://127.0.0.1:8642/health"): _FakeResponse(content=b'{"ok":true}'),
        ("GET", "http://127.0.0.1:9119/"): _FakeResponse(status_code=503, content=b"down", headers={"content-type": "text/plain"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["checks"]["api"]["ok"] is True
    assert payload["checks"]["dashboard"]["ok"] is False


def test_entrypoint_writes_explicit_context_length_override():
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert "HERMES_CONTEXT_LENGTH" in entrypoint
    assert "context_length: ${HERMES_CONTEXT_LENGTH}" in entrypoint
    assert "glm-5.1" in entrypoint
    assert "fallback_model:" in entrypoint
    assert "model: \"${HERMES_FALLBACK_MODEL}\"" in entrypoint
    assert ".hermes/skills" in entrypoint
    assert "for bundled_skill in /app/skills/*" in entrypoint
    assert 'cp -R "${bundled_skill}"' in entrypoint
    assert "TAVILY_API_KEY" in entrypoint
    assert "EXA_API_KEY" in entrypoint


def test_runtime_dockerfile_installs_browser_runtime_and_skills_assets():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "chromium" in dockerfile
    assert "ripgrep" in dockerfile
    assert "agent-browser" in dockerfile
    assert "/usr/local/lib/python3.12/site-packages/node_modules/agent-browser" in dockerfile
    assert "COPY skills ./skills" in dockerfile


def test_runtime_bundles_cn_search_and_kdocs_skills():
    skills_root = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "skills"
    )

    assert (skills_root / "multi-search-engine" / "SKILL.md").exists()
    assert (skills_root / "agent-browser-clawdbot" / "SKILL.md").exists()
    assert (skills_root / "kdocs" / "SKILL.md").exists()


def test_runtime_exec_allowlist_rejects_unsupported_config_query():
    module = _load_runtime_module()

    with pytest.raises(ValueError):
        module._validate_exec_argv(["config", "query"])

    with pytest.raises(ValueError):
        module._validate_exec_argv(["config", "query", "model.context_length"])


def test_runtime_pairing_allowlist_accepts_upstream_safe_commands():
    module = _load_runtime_module()

    assert module._validate_pairing_argv(["list"]) == ["list"]
    assert module._validate_pairing_argv(["approve", "feishu", "ABC123"]) == ["approve", "feishu", "ABC123"]
    assert module._validate_pairing_argv(["approve", "weixin", "ABC123"]) == ["approve", "weixin", "ABC123"]
    assert module._validate_pairing_argv(["revoke", "telegram", "user-1"]) == ["revoke", "telegram", "user-1"]
    assert module._validate_pairing_argv(["clear-pending"]) == ["clear-pending"]


def test_runtime_pairing_allowlist_rejects_unsafe_commands():
    module = _load_runtime_module()

    with pytest.raises(ValueError):
        module._validate_pairing_argv(["approve", "unknown-platform", "ABC123"])

    with pytest.raises(ValueError):
        module._validate_pairing_argv(["list", "--json"])

    with pytest.raises(ValueError):
        module._validate_pairing_argv(["approve", "feishu", "A;B"])


def test_terminal_ws_keeps_streaming_after_stdin_eof(monkeypatch):
    module = _load_runtime_module()
    master_fd, slave_fd = os.openpty()
    started = threading.Event()

    def _fake_fork():
        return 4321, master_fd

    async def _fake_wait_process(_pid: int) -> int:
        await module.asyncio.sleep(0.15)
        return 0

    def _writer():
        started.wait(timeout=1)
        time.sleep(0.02)
        os.write(slave_fd, b"doctor ok\n")
        time.sleep(0.02)
        os.close(slave_fd)

    writer_thread = threading.Thread(target=_writer, daemon=True)
    writer_thread.start()

    monkeypatch.setattr(module.pty, "fork", _fake_fork)
    monkeypatch.setattr(module, "_wait_process", _fake_wait_process)

    with TestClient(module.app) as client:
        with client.websocket_connect(
            "/_ksadk/terminal/ws",
            subprotocols=[module.TERMINAL_SUBPROTOCOL],
        ) as websocket:
            websocket.send_text(
                json.dumps(
                    {
                        "type": "start",
                        "mode": "exec",
                        "argv": ["doctor"],
                        "cols": 80,
                        "rows": 24,
                    }
                )
            )
            ready = json.loads(websocket.receive_text())
            assert ready["type"] == "ready"

            started.set()
            websocket.send_text(json.dumps({"type": "stdin_eof"}))

            assert websocket.receive_bytes() == b"doctor ok\r\n"
            exit_payload = json.loads(websocket.receive_text())
            assert exit_payload == {"type": "exit", "code": 0}

    writer_thread.join(timeout=1)
