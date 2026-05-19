from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "hermes"
    / "runtime"
    / "app.py"
)
HOSTED_GATEWAY_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "hermes"
    / "runtime"
    / "hosted_gateway.py"
)


def _load_runtime_module():
    if str(MODULE_PATH.parent) not in sys.path:
        sys.path.insert(0, str(MODULE_PATH.parent))
    sys.modules.pop("workspace_files", None)
    spec = importlib.util.spec_from_file_location("ksadk_hermes_runtime_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hosted_gateway_module():
    spec = importlib.util.spec_from_file_location("ksadk_hosted_gateway_test", HOSTED_GATEWAY_MODULE_PATH)
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


def test_runtime_exposes_workspace_files_from_hermes_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("KSADK_WORKSPACE_FILES_ENABLED", "1")
    module = _load_runtime_module()

    with TestClient(module.app) as client:
        upload_response = client.post(
            "/_ksadk/workspace/v1/files/notes/todo.txt",
            files={"file": ("todo.txt", b"remember me", "text/plain")},
        )
        list_response = client.get("/_ksadk/workspace/v1/entries", params={"path": "notes"})
        download_response = client.get("/_ksadk/workspace/v1/files/notes/todo.txt")
        delete_response = client.delete("/_ksadk/workspace/v1/files/notes/todo.txt")

    assert upload_response.status_code == 200
    assert upload_response.json()["Entry"]["Path"] == "notes/todo.txt"
    assert list_response.status_code == 200
    assert list_response.json()["Entries"] == [
        {
            "Name": "todo.txt",
            "Path": "notes/todo.txt",
            "Type": "file",
            "SizeBytes": 11,
            "MimeType": "text/plain",
            "ModifiedAt": list_response.json()["Entries"][0]["ModifiedAt"],
        }
    ]
    assert download_response.status_code == 200
    assert download_response.content == b"remember me"
    assert delete_response.status_code == 200
    assert delete_response.json() == {"Deleted": True}


def test_runtime_module_imports_without_repo_ksadk_package(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    shutil.copy2(MODULE_PATH, runtime_dir / "app.py")
    runtime_common_src = MODULE_PATH.parents[3] / "ksadk_runtime_common"
    opt_dir = tmp_path / "opt"
    shutil.copytree(runtime_common_src, opt_dir / "ksadk_runtime_common")

    monkeypatch.setitem(sys.modules, "ksadk", None)
    for module_name in list(sys.modules):
        if module_name == "ksadk_runtime_common" or module_name.startswith("ksadk_runtime_common."):
            sys.modules.pop(module_name, None)
    monkeypatch.syspath_prepend(str(runtime_dir))
    monkeypatch.syspath_prepend(str(opt_dir))

    spec = importlib.util.spec_from_file_location("isolated_ksadk_hermes_runtime", runtime_dir / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert getattr(module, "app", None) is not None


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
    assert "dashboard" in response.text
    assert "__KSADK_HERMES_FETCH_SHIM__" in response.text
    method, url, _headers, _content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:9119/"
    assert stream is True


def test_proxy_dashboard_translates_session_header_back_to_authorization(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:9119/api/sessions?limit=1",
        ): _FakeResponse(status_code=200, content=b'{"sessions":[]}', headers={"content-type": "application/json"}),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get(
            "/api/sessions?limit=1",
            headers={"X-Hermes-Session-Token": "demo-token"},
        )

    assert response.status_code == 200
    method, url, headers, _content, stream = _FakeAsyncClient.send_calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:9119/api/sessions?limit=1"
    assert stream is True
    assert headers is not None
    assert headers["Authorization"] == "Bearer demo-token"
    assert "X-Hermes-Session-Token" not in headers


def test_runtime_blocks_dashboard_self_update_for_hosted_pods(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.post("/api/hermes/update")

    assert response.status_code == 409
    assert "重新部署" in response.text
    assert _FakeAsyncClient.send_calls == []


def test_runtime_exposes_terminal_session_control_plane(monkeypatch):
    module = _load_runtime_module()
    monkeypatch.setattr(module, "_spawn_terminal_session", lambda session: None)

    with TestClient(module.app) as client:
        create_response = client.post(
            "/_ksadk/terminal/sessions",
            json={"mode": "tui", "cols": 96, "rows": 28, "cwd": "workspace-a"},
        )
        list_response = client.get("/_ksadk/terminal/sessions")

    assert create_response.status_code == 200
    created = create_response.json()["session"]
    assert created["terminal_session_id"].startswith("term-")
    assert created["mode"] == "tui"
    assert created["status"] == "running"
    assert created["cols"] == 96
    assert created["rows"] == 28
    assert created["cwd"] == "workspace-a"
    assert list_response.status_code == 200
    assert list_response.json()["sessions"][0]["terminal_session_id"] == created["terminal_session_id"]


def test_runtime_closes_terminal_session(monkeypatch):
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


def test_proxy_dashboard_injects_fetch_shim_into_html(monkeypatch):
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:9119/",
        ): _FakeResponse(
            status_code=200,
            content=b'<html><head><script>window.__HERMES_SESSION_TOKEN__="demo-token";</script></head><body>dashboard</body></html>',
            headers={"content-type": "text/html; charset=utf-8"},
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "__KSADK_HERMES_FETCH_SHIM__" in response.text
    assert "X-Hermes-Session-Token" in response.text
    assert 'window.__HERMES_SESSION_TOKEN__="demo-token"' in response.text


def test_proxy_dashboard_injects_default_zh_locale_bootstrap(monkeypatch):
    monkeypatch.delenv("HERMES_UI_LOCALE", raising=False)
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:9119/",
        ): _FakeResponse(
            status_code=200,
            content=b"<html><head></head><body>dashboard</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "hermes-locale" in response.text
    assert "zh" in response.text


def test_proxy_dashboard_injects_explicit_en_locale_bootstrap(monkeypatch):
    monkeypatch.setenv("HERMES_UI_LOCALE", "en_US.UTF-8")
    module = _load_runtime_module()
    _FakeAsyncClient.send_calls = []
    _FakeAsyncClient.routes = {
        (
            "GET",
            "http://127.0.0.1:9119/",
        ): _FakeResponse(
            status_code=200,
            content=b"<html><head></head><body>dashboard</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        ),
    }
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    with TestClient(module.app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "hermes-locale" in response.text
    assert "en" in response.text


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


def test_runtime_promotes_dumb_term_to_xterm_256color(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")

    _load_runtime_module()

    assert os.environ["TERM"] == "xterm-256color"


def test_entrypoint_writes_explicit_context_length_override():
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert "HERMES_CONTEXT_LENGTH" in entrypoint
    assert "context_length: ${HERMES_CONTEXT_LENGTH}" in entrypoint
    assert "HERMES_COMPRESSION_CONTEXT_LENGTH" in entrypoint
    assert "auxiliary:" in entrypoint
    assert "compression:" in entrypoint
    assert "context_length: ${HERMES_COMPRESSION_CONTEXT_LENGTH}" in entrypoint
    assert "glm-5.1" in entrypoint
    assert "fallback_model:" in entrypoint
    assert "model: \"${HERMES_FALLBACK_MODEL}\"" in entrypoint
    assert '${HERMES_HOME}/skills' in entrypoint
    assert "for bundled_skill in /app/skills/*" in entrypoint
    assert 'cp -R "${bundled_skill}"' in entrypoint
    assert "TAVILY_API_KEY" in entrypoint
    assert "EXA_API_KEY" in entrypoint
    assert 'export HOME="${HOME:-/home/node}"' in entrypoint
    assert 'export HERMES_STATE_DIR="${HERMES_STATE_DIR:-${HOME}/.hermes}"' in entrypoint
    assert 'export HERMES_HOME="${HERMES_HOME:-${HERMES_STATE_DIR}}"' in entrypoint
    assert 'export HERMES_WORKDIR="${HERMES_WORKDIR:-${HERMES_HOME}/workspace}"' in entrypoint
    assert 'export HERMES_RUN_DIR="${HERMES_RUN_DIR:-${HERMES_HOME}/run}"' in entrypoint
    assert 'export HERMES_SESSION_DIR="${HERMES_SESSION_DIR:-${HERMES_HOME}/sessions}"' in entrypoint
    assert 'export HERMES_HOSTED_RUNTIME="${HERMES_HOSTED_RUNTIME:-1}"' in entrypoint
    assert 'export KSADK_WORKSPACE_ROOT="${KSADK_WORKSPACE_ROOT:-${HERMES_WORKDIR}}"' in entrypoint
    assert 'export KSADK_WORKSPACE_FILES_ENABLED="${KSADK_WORKSPACE_FILES_ENABLED:-1}"' in entrypoint
    assert 'if [[ -z "${TERM:-}" || "${TERM}" == "dumb" ]]; then' in entrypoint
    assert 'export TERM="xterm-256color"' in entrypoint
    assert 'export PYTHONPATH="/app/runtime${PYTHONPATH:+:${PYTHONPATH}}"' in entrypoint
    assert 'export AGENT_BROWSER_HOME="${AGENT_BROWSER_HOME:-/usr/local/lib/node_modules/agent-browser}"' in entrypoint
    assert 'export MCPORTER_HOME="${MCPORTER_HOME:-${HERMES_HOME}/mcporter}"' in entrypoint
    assert 'export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HERMES_HOME}/xdg/config}"' in entrypoint
    assert 'export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${HERMES_HOME}/xdg/cache}"' in entrypoint
    assert 'export XDG_STATE_HOME="${XDG_STATE_HOME:-${HERMES_HOME}/xdg/state}"' in entrypoint
    assert 'export AGENT_BROWSER_STATE_DIR="${AGENT_BROWSER_STATE_DIR:-${HERMES_HOME}/browser}"' in entrypoint
    assert 'export AGENT_BROWSER_RUN_DIR="${AGENT_BROWSER_RUN_DIR:-${AGENT_BROWSER_STATE_DIR}/run}"' in entrypoint
    assert 'export AGENT_BROWSER_SESSION_DIR="${AGENT_BROWSER_SESSION_DIR:-${AGENT_BROWSER_STATE_DIR}/sessions}"' in entrypoint
    assert 'export AGENT_BROWSER_SOCKET_DIR="${AGENT_BROWSER_SOCKET_DIR:-${AGENT_BROWSER_RUN_DIR}}"' in entrypoint
    assert 'export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"' in entrypoint
    assert 'export KDOCS_OPEN_BROWSER="${KDOCS_OPEN_BROWSER:-0}"' in entrypoint
    assert 'export HERMES_UI_LOCALE="${HERMES_UI_LOCALE:-zh}"' in entrypoint
    assert 'export HERMES_TUI_PREWARM="${HERMES_TUI_PREWARM:-true}"' in entrypoint
    assert 'export HERMES_TUI_PREWARM_TIMEOUT="${HERMES_TUI_PREWARM_TIMEOUT:-75}"' in entrypoint
    assert 'export TIRITH_ENABLED="${TIRITH_ENABLED:-false}"' in entrypoint
    assert 'GATEWAY_PID_FILE="${HERMES_RUN_DIR}/gateway.pid"' in entrypoint
    assert 'start_gateway_process() {' in entrypoint
    assert "prewarm_hermes_tui() {" in entrypoint
    assert "Hermes TUI prewarm starting" in entrypoint
    assert 'timeout "${HERMES_TUI_PREWARM_TIMEOUT}" script -q -c "hermes chat" /dev/null' in entrypoint
    assert 'while true; do' in entrypoint
    assert 'GATEWAY_LOCAL_RESTART_MAX="${GATEWAY_LOCAL_RESTART_MAX:-5}"' in entrypoint
    assert 'GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS="${GATEWAY_LOCAL_RESTART_BACKOFF_SECONDS:-2}"' in entrypoint
    assert 'kill -TERM "${MAIN_PID}"' in entrypoint
    assert 'mkdir -p "${HERMES_WORKDIR}"' in entrypoint
    assert 'mkdir -p "${HERMES_HOME}" "${HERMES_HOME}/skills" "${HERMES_RUN_DIR}" "${HERMES_SESSION_DIR}"' in entrypoint
    assert 'mkdir -p "${AGENT_BROWSER_STATE_DIR}" "${AGENT_BROWSER_RUN_DIR}" "${AGENT_BROWSER_SESSION_DIR}"' in entrypoint
    assert 'cd "${HERMES_WORKDIR}"' in entrypoint
    assert 'enabled: ${API_SERVER_ENABLED}' in entrypoint
    assert 'security:' in entrypoint
    assert 'tirith_enabled: ${TIRITH_ENABLED}' in entrypoint
    assert 'tirith_path: "tirith"' in entrypoint
    assert 'tirith_timeout: 5' in entrypoint
    assert 'tirith_fail_open: true' in entrypoint
    assert 'export HERMES_UI_LOCALE="$(normalize_hermes_ui_locale "${HERMES_UI_LOCALE}")"' in entrypoint
    assert "HERMES_UI_LOCALE" in entrypoint
    assert "TIRITH_ENABLED=${TIRITH_ENABLED}" in entrypoint


def test_entrypoint_auto_enables_langfuse_plugin_when_credentials_exist():
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert 'export HERMES_LANGFUSE_PUBLIC_KEY="${HERMES_LANGFUSE_PUBLIC_KEY:-${LANGFUSE_PUBLIC_KEY:-}}"' in entrypoint
    assert 'export HERMES_LANGFUSE_SECRET_KEY="${HERMES_LANGFUSE_SECRET_KEY:-${LANGFUSE_SECRET_KEY:-}}"' in entrypoint
    assert 'export HERMES_LANGFUSE_BASE_URL="${HERMES_LANGFUSE_BASE_URL:-${LANGFUSE_BASE_URL:-${LANGFUSE_HOST:-}}}"' in entrypoint
    assert "HERMES_LANGFUSE_PUBLIC_KEY=${HERMES_LANGFUSE_PUBLIC_KEY}" in entrypoint
    assert "HERMES_LANGFUSE_SECRET_KEY=${HERMES_LANGFUSE_SECRET_KEY}" in entrypoint
    assert 'HERMES_LANGFUSE_AUTO_ENABLE="${HERMES_LANGFUSE_AUTO_ENABLE:-true}"' in entrypoint
    assert 'if [[ -n "${HERMES_LANGFUSE_PUBLIC_KEY}" && -n "${HERMES_LANGFUSE_SECRET_KEY}" ]]; then' in entrypoint
    assert "plugins:" in entrypoint
    assert "observability/langfuse" in entrypoint
    assert "from hermes_cli.plugins_cmd import _get_enabled_set, _save_enabled_set" in entrypoint
    assert 'enabled.add("observability/langfuse")' in entrypoint
    assert "_save_enabled_set(enabled)" in entrypoint
    assert "Langfuse plugin enabled: observability/langfuse" in entrypoint
    assert '0|false|no|off)' in entrypoint


def test_entrypoint_runs_uvicorn_with_explicit_app_dir():
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert 'exec uvicorn --app-dir /app runtime.app:app --host 0.0.0.0 --port "${PORT}"' in entrypoint


def test_entrypoint_does_not_patch_hermes_package_files():
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "entrypoint.sh"
    ).read_text(encoding="utf-8")

    assert "web_dist/index.html" not in entrypoint
    assert "__HERMES_UI_LOCALE_BOOTSTRAP__" not in entrypoint
    assert 'mv "${tmp_out}" "${control_ui_index}"' not in entrypoint


def test_runtime_bundles_hosted_gateway_patches():
    runtime_root = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "runtime"
    )

    sitecustomize = (runtime_root / "sitecustomize.py").read_text(encoding="utf-8")
    hosted_gateway = (runtime_root / "hosted_gateway.py").read_text(encoding="utf-8")
    app_py = (runtime_root / "app.py").read_text(encoding="utf-8")

    assert "HERMES_HOSTED_RUNTIME" in sitecustomize
    assert "apply_hosted_patches" in sitecustomize
    assert "gateway_setup" in hosted_gateway
    assert "gateway.pid" in hosted_gateway
    assert "container-managed" in hosted_gateway
    assert "from ksadk_runtime_common.workspace_files import" in app_py


def test_hosted_gateway_command_falls_back_to_original_handler_without_recursing():
    module = _load_hosted_gateway_module()
    calls: list[str] = []

    def _original(args):
        calls.append(args.gateway_command)

    module._ORIGINAL_GATEWAY_COMMAND = _original

    module.hosted_gateway_command(SimpleNamespace(gateway_command="run"))

    assert calls == ["run"]


def test_hosted_gateway_command_exits_cleanly_on_keyboard_interrupt(monkeypatch):
    module = _load_hosted_gateway_module()

    def _raise_keyboard_interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "hosted_gateway_setup", _raise_keyboard_interrupt)

    with pytest.raises(SystemExit) as exc:
        module.hosted_gateway_command(SimpleNamespace(gateway_command="setup"))

    assert exc.value.code == 130


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
    assert "ARG KDOCS_SKILL_REPO=https://github.com/kdocs-app/kdocs-skill.git" in dockerfile
    assert 'git clone --depth 1 "${KDOCS_SKILL_REPO}" /tmp/kdocs-skill' in dockerfile
    assert 'cp -R /tmp/kdocs-skill /app/skills/kdocs' in dockerfile
    assert "COPY ksadk_runtime_common /opt/ksadk_runtime_common" in dockerfile
    assert "/usr/local/bin/npm" in dockerfile
    assert "/usr/local/bin/npx" in dockerfile
    assert "/usr/local/bin/mcporter" in dockerfile


def test_runtime_bundles_cn_search_and_kdocs_skills():
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "COPY deploy/hermes/skills/multi-search-engine ./skills/multi-search-engine" in dockerfile
    assert "COPY deploy/hermes/skills/agent-browser-clawdbot ./skills/agent-browser-clawdbot" in dockerfile
    assert "COPY --from=kdocs_skill /app/skills/kdocs ./skills/kdocs" in dockerfile


def test_runtime_readme_documents_single_persistent_home_layout():
    readme = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "README.md"
    ).read_text(encoding="utf-8")

    assert "~/.hermes" in readme
    assert "HOME=/home/node" in readme
    assert "HERMES_HOME=/home/node/.hermes" in readme
    assert "HERMES_WORKDIR=/home/node/.hermes/workspace" in readme
    assert "AGENT_BROWSER_HOME=/usr/local/lib/node_modules/agent-browser" in readme
    assert "AGENT_BROWSER_STATE_DIR=/home/node/.hermes/browser" in readme
    assert "AGENT_BROWSER_SOCKET_DIR=/home/node/.hermes/browser/run" in readme
    assert "single persistent directory" in readme.lower()


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


def test_runtime_terminal_command_supports_connect_mode():
    module = _load_runtime_module()

    assert module._resolve_terminal_command("connect", []) == ["hermes", "gateway", "setup"]


def test_runtime_terminal_cwd_resolves_under_workspace(monkeypatch, tmp_path):
    workspace_dir = tmp_path / "hermes-home" / "workspace" / "demo-workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    module = _load_runtime_module()

    assert module._resolve_terminal_cwd("demo-workspace") == workspace_dir.resolve()


def test_runtime_terminal_cwd_rejects_workspace_escape(monkeypatch, tmp_path):
    workspace_root = tmp_path / "hermes-home" / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    module = _load_runtime_module()

    with pytest.raises(ValueError):
        module._resolve_terminal_cwd("../outside")


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
