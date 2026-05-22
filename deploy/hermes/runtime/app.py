import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import os
import pty
import select
import signal
import termios
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketState

from ksadk_runtime_common.workspace_files import (
    create_workspace_files_router,
    workspace_files_enabled,
)


TERMINAL_SUBPROTOCOL = "ks-terminal.v1"
HERMES_SESSION_PROXY_HEADER = "X-Hermes-Session-Token"
HERMES_UI_LOCALE_BOOTSTRAP_ID = "__KSADK_HERMES_UI_LOCALE_BOOTSTRAP__"
SHELL_METACHARS = set("|&;<>()$`\\\\\n\r")
PAIRING_PLATFORMS = {
    "discord",
    "dingtalk",
    "email",
    "feishu",
    "homeassistant",
    "mattermost",
    "matrix",
    "signal",
    "slack",
    "telegram",
    "wecom",
    "wecom_callback",
    "webhook",
    "weixin",
    "whatsapp",
}
SINGLE_READONLY = {"status", "doctor", "version", "insights"}
NESTED_READONLY = {
    "sessions": {"list": (2, 2), "show": (3, 3), "export": (3, 3)},
    "config": {"show": (2, 2), "check": (2, 2), "path": (2, 2), "env-path": (2, 2)},
    "skills": {"list": (2, 2), "audit": (2, 2), "check": (2, 2)},
    "tools": {"list": (2, 2)},
    "cron": {"list": (2, 2), "status": (2, 2)},
    "gateway": {"status": (2, 2)},
}

app = FastAPI()


@dataclass
class TerminalSession:
    id: str
    mode: str = "tui"
    argv: list[str] = field(default_factory=list)
    cols: int = 80
    rows: int = 24
    cwd: str = ""
    status: str = "starting"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pid: int | None = None
    fd: int | None = None
    exit_code: int | None = None
    transient: bool = False
    reader_task: asyncio.Task | None = None
    wait_task: asyncio.Task | None = None
    attachments: set[WebSocket] = field(default_factory=set)
    replay_buffer: bytearray = field(default_factory=bytearray)


TERMINAL_REPLAY_BUFFER_BYTES = 256 * 1024
_TERMINAL_SESSIONS: dict[str, TerminalSession] = {}


def _utc_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _serialize_terminal_session(session: TerminalSession) -> dict:
    return {
        "terminal_session_id": session.id,
        "mode": session.mode,
        "status": session.status,
        "cols": session.cols,
        "rows": session.rows,
        "cwd": session.cwd,
        "created_at": _utc_timestamp(session.created_at),
        "updated_at": _utc_timestamp(session.updated_at),
        "exit_code": session.exit_code,
    }


def _append_terminal_replay(session: TerminalSession, data: bytes) -> None:
    session.replay_buffer.extend(data)
    overflow = len(session.replay_buffer) - TERMINAL_REPLAY_BUFFER_BYTES
    if overflow > 0:
        del session.replay_buffer[:overflow]


async def _broadcast_terminal_bytes(session: TerminalSession, data: bytes) -> None:
    _append_terminal_replay(session, data)
    stale: list[WebSocket] = []
    for attached in list(session.attachments):
        try:
            await attached.send_bytes(data)
        except Exception:
            stale.append(attached)
    for attached in stale:
        session.attachments.discard(attached)


async def _broadcast_terminal_control(session: TerminalSession, payload: dict) -> None:
    stale: list[WebSocket] = []
    text = json.dumps(payload, ensure_ascii=False)
    for attached in list(session.attachments):
        try:
            await attached.send_text(text)
        except Exception:
            stale.append(attached)
    for attached in stale:
        session.attachments.discard(attached)


def _workspace_root() -> Path:
    return Path(
        os.getenv("KSADK_WORKSPACE_ROOT")
        or os.getenv("HERMES_WORKDIR")
        or (Path(os.getenv("HERMES_HOME", "/home/node/.hermes")) / "workspace")
    )


def _resolve_terminal_cwd(cwd: str | None) -> Path | None:
    raw = str(cwd or "").strip().replace("\\", "/")
    if not raw:
        return None

    workspace_root = _workspace_root().resolve()
    if raw in {".", "/"}:
        resolved = workspace_root
    else:
        parts = [part for part in PurePosixPath(raw.lstrip("/")).parts if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise ValueError("workspace cwd escapes the workspace root")
        resolved = workspace_root.joinpath(*parts).resolve()

    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("workspace cwd escapes the workspace root") from exc
    if not resolved.exists():
        raise ValueError(f"workspace cwd does not exist: {raw}")
    if not resolved.is_dir():
        raise ValueError(f"workspace cwd is not a directory: {raw}")
    return resolved

app.include_router(
    create_workspace_files_router(
        root_getter=_workspace_root,
        enabled_getter=lambda: workspace_files_enabled(default=True),
    )
)

os.environ.setdefault("HERMES_HOSTED_RUNTIME", "1")


def _normalize_term_env() -> None:
    current = str(os.getenv("TERM", "")).strip().lower()
    if current in {"", "dumb"}:
        os.environ["TERM"] = "xterm-256color"


_normalize_term_env()

HERMES_FETCH_SHIM = f"""<script id="__KSADK_HERMES_FETCH_SHIM__">
(() => {{
  const HEADER = "{HERMES_SESSION_PROXY_HEADER}";
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {{
    const request = input instanceof Request ? input : null;
    let url;
    try {{
      url = new URL(request ? request.url : String(input), window.location.href);
    }} catch (_error) {{
      return originalFetch(input, init);
    }}
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) {{
      return originalFetch(input, init);
    }}
    const headers = new Headers((init && init.headers) || (request ? request.headers : undefined));
    const requestAuth = request ? (request.headers.get("Authorization") || request.headers.get("authorization") || "") : "";
    const auth = headers.get("Authorization") || headers.get("authorization") || requestAuth;
    let token = "";
    if (auth && auth.startsWith("Bearer ")) {{
      token = auth.slice(7);
    }} else if (window.__HERMES_SESSION_TOKEN__) {{
      token = window.__HERMES_SESSION_TOKEN__;
    }}
    if (!token) {{
      return originalFetch(input, init);
    }}
    headers.delete("Authorization");
    headers.delete("authorization");
    headers.set(HEADER, token);
    if (request) {{
      return originalFetch(new Request(request, {{ headers }}));
    }}
    return originalFetch(input, {{ ...(init || {{}}), headers }});
  }};
}})();
</script>"""


def _api_base() -> str:
    return f"http://{os.getenv('API_SERVER_HOST', '127.0.0.1')}:{os.getenv('API_SERVER_PORT', '8642')}"


def _dashboard_base() -> str:
    return f"http://{os.getenv('HERMES_DASHBOARD_HOST', '127.0.0.1')}:{os.getenv('HERMES_DASHBOARD_PORT', '9119')}"


def _normalize_hermes_ui_locale(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return "zh"

    base = text.split(".", 1)[0].replace("_", "-").strip().lower()
    if base in {"c", "c-utf-8", "c.utf-8", "posix"}:
        return "zh"
    if base.startswith("en"):
        return "en"
    if base.startswith("zh"):
        return "zh"
    return "zh"


def _dashboard_locale_bootstrap() -> str:
    ui_locale = _normalize_hermes_ui_locale(os.getenv("HERMES_UI_LOCALE"))
    escaped = ui_locale.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f'<script id="{HERMES_UI_LOCALE_BOOTSTRAP_ID}">'
        f"try{{if(!localStorage.getItem('hermes-locale')){{localStorage.setItem('hermes-locale','{escaped}');}}}}catch(_error){{}}"
        "</script>"
    )


def _is_dashboard_api_path(path: str) -> bool:
    normalized = path.strip("/")
    return normalized == "api" or normalized.startswith("api/")


def _rewrite_dashboard_request_headers(headers: dict[str, str], path: str) -> dict[str, str]:
    if not _is_dashboard_api_path(path):
        return headers

    token = ""
    for key in list(headers):
        if key.lower() == HERMES_SESSION_PROXY_HEADER.lower():
            token = headers.pop(key)
            break

    if token and not any(key.lower() == "authorization" for key in headers):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _inject_dashboard_fetch_shim(body: bytes, content_type: str) -> bytes:
    if "text/html" not in (content_type or "").lower():
        return body

    html = body.decode("utf-8")
    injections: list[str] = []
    if "__KSADK_HERMES_FETCH_SHIM__" not in html:
        injections.append(HERMES_FETCH_SHIM)
    if HERMES_UI_LOCALE_BOOTSTRAP_ID not in html:
        injections.append(_dashboard_locale_bootstrap())
    if not injections:
        return body

    combined = "".join(injections)
    if "</head>" in html:
        html = html.replace("</head>", f"{combined}</head>", 1)
    else:
        html = f"{combined}{html}"
    return html.encode("utf-8")


def _validate_exec_argv(argv: Iterable[str]) -> list[str]:
    normalized = [str(item).strip() for item in argv]
    if not normalized:
        raise ValueError("missing argv")
    for item in normalized:
        if not item or item.startswith("-") or any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"unsafe argv: {item}")
    if normalized[0] in SINGLE_READONLY:
        if len(normalized) != 1:
            raise ValueError("unsupported argv")
        return normalized
    nested = NESTED_READONLY.get(normalized[0])
    if not nested or len(normalized) < 2:
        raise ValueError("unsupported argv")
    bounds = nested.get(normalized[1])
    if not bounds:
        raise ValueError("unsupported argv")
    if not (bounds[0] <= len(normalized) <= bounds[1]):
        raise ValueError("unsupported argv")
    return normalized


def _validate_pairing_argv(argv: Iterable[str]) -> list[str]:
    normalized = [str(item).strip() for item in argv]
    if not normalized:
        raise ValueError("missing argv")
    for item in normalized:
        if not item or item.startswith("-") or any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"unsafe argv: {item}")

    action = normalized[0]
    if action in {"list", "clear-pending"}:
        if len(normalized) != 1:
            raise ValueError("unsupported argv")
        return normalized
    if action in {"approve", "revoke"}:
        if len(normalized) != 3:
            raise ValueError("unsupported argv")
        platform = normalized[1].lower()
        if platform not in PAIRING_PLATFORMS:
            raise ValueError("unsupported argv")
        normalized[1] = platform
        return normalized
    raise ValueError("unsupported argv")


def _resolve_terminal_command(mode: str, argv: Iterable[str]) -> list[str]:
    normalized_mode = str(mode or "").strip().lower()
    normalized_argv = [str(item).strip() for item in argv]
    if normalized_mode == "tui":
        return ["hermes", "chat"]
    if normalized_mode == "exec":
        return ["hermes", *_validate_exec_argv(normalized_argv)]
    if normalized_mode == "pairing":
        return ["hermes", "pairing", *_validate_pairing_argv(normalized_argv)]
    if normalized_mode == "connect":
        if normalized_argv:
            raise ValueError("unsupported argv")
        return ["hermes", "gateway", "setup"]
    raise ValueError("unsupported mode")


async def _proxy_http(request: Request, base_url: str, path: str) -> Response:
    target = f"{base_url}/{path.lstrip('/')}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    if base_url == _dashboard_base():
        headers = _rewrite_dashboard_request_headers(headers, path)
    client = httpx.AsyncClient(timeout=None)
    upstream = await client.send(
        client.build_request(
            request.method,
            target,
            headers=headers,
            content=await request.body(),
        ),
        stream=True,
    )
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}
    }

    async def _close_proxy_stream() -> None:
        await upstream.aclose()
        await client.aclose()

    if (upstream.headers.get("content-type") or "").startswith("text/event-stream"):
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(_close_proxy_stream),
        )

    try:
        body = await upstream.aread()
    finally:
        await upstream.aclose()
        await client.aclose()
    if base_url == _dashboard_base():
        body = _inject_dashboard_fetch_shim(body, upstream.headers.get("content-type", ""))
    return Response(body, status_code=upstream.status_code, headers=response_headers)


async def _health_check(name: str, url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.request("GET", url)
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "status_code": None,
            "url": url,
            "error": str(exc),
        }
    return {
        "name": name,
        "ok": 200 <= int(response.status_code) < 400,
        "status_code": int(response.status_code),
        "url": url,
    }


@app.get("/health")
async def health() -> Response:
    checks = {
        "api": await _health_check("api", f"{_api_base()}/health"),
        "dashboard": await _health_check("dashboard", f"{_dashboard_base()}/"),
    }
    ok = all(check["ok"] for check in checks.values())
    return JSONResponse(
        {
            "ok": ok,
            "checks": checks,
        },
        status_code=200 if ok else 503,
    )


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_api(path: str, request: Request) -> Response:
    return await _proxy_http(request, _api_base(), f"v1/{path}")


@app.post("/api/hermes/update")
async def block_dashboard_self_update() -> Response:
    return Response(
        "Hermes 自更新在 AgentEngine 托管运行时中已禁用。请升级运行时镜像并重新部署 Hermes Agent。",
        status_code=409,
        media_type="text/plain",
    )


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    termios.tcsetwinsize(fd, (int(rows or 24), int(cols or 80)))


async def _pty_reader(ws: WebSocket, fd: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, lambda: select.select([fd], [], [], None))
        data = os.read(fd, 4096)
        if not data:
            return
        await ws.send_bytes(data)


async def _wait_process(pid: int) -> int:
    loop = asyncio.get_running_loop()
    _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return status


async def _terminal_session_reader(session: TerminalSession) -> None:
    if session.fd is None:
        return
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, lambda: select.select([session.fd], [], [], None))
        try:
            data = os.read(session.fd, 4096)
        except OSError:
            return
        if not data:
            return
        await _broadcast_terminal_bytes(session, data)


async def _terminal_session_waiter(session: TerminalSession) -> None:
    if session.pid is None:
        return
    code = await _wait_process(session.pid)
    session.exit_code = code
    session.status = "closed"
    session.updated_at = time.time()
    await _broadcast_terminal_control(session, {"type": "exit", "code": code})
    if session.fd is not None:
        with contextlib.suppress(OSError):
            os.close(session.fd)
        session.fd = None


def _spawn_terminal_session(session: TerminalSession) -> None:
    if session.pid is not None:
        return
    command = _resolve_terminal_command(session.mode, session.argv)
    terminal_cwd = _resolve_terminal_cwd(session.cwd)
    pid, fd = pty.fork()
    if pid == 0:
        if terminal_cwd is not None:
            os.chdir(str(terminal_cwd))
        os.execvp(command[0], command)
    session.pid = pid
    session.fd = fd
    session.status = "running"
    session.updated_at = time.time()
    _set_winsize(fd, session.rows, session.cols)
    session.reader_task = asyncio.create_task(_terminal_session_reader(session))
    session.wait_task = asyncio.create_task(_terminal_session_waiter(session))


def _terminate_terminal_session(session: TerminalSession) -> None:
    if session.pid is not None and session.status not in {"closed", "exited"}:
        with contextlib.suppress(ProcessLookupError):
            os.kill(session.pid, signal.SIGTERM)
    for task in (session.reader_task,):
        if task and not task.done():
            task.cancel()
    if session.fd is not None:
        with contextlib.suppress(OSError):
            os.close(session.fd)
        session.fd = None


def _create_terminal_session(payload: dict) -> TerminalSession:
    mode = str(payload.get("mode") or "tui").strip().lower()
    argv = [str(item) for item in (payload.get("argv") or [])]
    _resolve_terminal_command(mode, argv)
    session = TerminalSession(
        id=f"term-{uuid.uuid4().hex[:12]}",
        mode=mode,
        argv=argv,
        cols=int(payload.get("cols") or 80),
        rows=int(payload.get("rows") or 24),
        cwd=str(payload.get("cwd") or "").strip(),
    )
    _TERMINAL_SESSIONS[session.id] = session
    _spawn_terminal_session(session)
    if session.status == "starting":
        session.status = "running"
        session.updated_at = time.time()
    return session


@app.post("/_ksadk/terminal/sessions")
async def create_terminal_session(request: Request) -> JSONResponse:
    payload = await request.json()
    session = _create_terminal_session(payload if isinstance(payload, dict) else {})
    return JSONResponse({"session": _serialize_terminal_session(session)})


@app.get("/_ksadk/terminal/sessions")
async def list_terminal_sessions() -> JSONResponse:
    sessions = sorted(
        _TERMINAL_SESSIONS.values(),
        key=lambda item: (item.status == "running", item.updated_at),
        reverse=True,
    )
    return JSONResponse({"sessions": [_serialize_terminal_session(session) for session in sessions]})


@app.delete("/_ksadk/terminal/sessions/{terminal_session_id}")
async def close_terminal_session(terminal_session_id: str) -> JSONResponse:
    session = _TERMINAL_SESSIONS.pop(terminal_session_id, None)
    if not session:
        return JSONResponse({"closed": False, "terminal_session_id": terminal_session_id}, status_code=404)
    _terminate_terminal_session(session)
    session.status = "closed"
    session.updated_at = time.time()
    await _broadcast_terminal_control(session, {"type": "exit", "code": session.exit_code})
    return JSONResponse({"closed": True, "terminal_session_id": terminal_session_id})


async def _attach_terminal_session(ws: WebSocket, session: TerminalSession) -> None:
    session.attachments.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "ready", "terminal_session_id": session.id}))
        if session.replay_buffer:
            await ws.send_bytes(bytes(session.replay_buffer))
        receive_task = asyncio.create_task(ws.receive())
        while True:
            message = await receive_task
            receive_task = asyncio.create_task(ws.receive())
            if message.get("bytes") is not None:
                if session.fd is not None and session.status == "running":
                    os.write(session.fd, message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            control = json.loads(text)
            if control.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif control.get("type") == "resize":
                session.rows = int(control.get("rows") or session.rows or 24)
                session.cols = int(control.get("cols") or session.cols or 80)
                session.updated_at = time.time()
                if session.fd is not None:
                    _set_winsize(session.fd, session.rows, session.cols)
            elif control.get("type") == "signal" and session.pid is not None:
                sig = signal.SIGINT if control.get("signal") == "SIGINT" else signal.SIGTERM
                os.kill(session.pid, sig)
            elif control.get("type") == "stdin_eof":
                continue
    finally:
        session.attachments.discard(ws)


@app.websocket("/_ksadk/terminal/ws")
async def terminal_ws(ws: WebSocket) -> None:
    if TERMINAL_SUBPROTOCOL not in (ws.headers.get("sec-websocket-protocol") or ""):
        await ws.close(code=4400, reason="missing ks-terminal.v1 subprotocol")
        return
    await ws.accept(subprotocol=TERMINAL_SUBPROTOCOL)
    pid = None
    fd = None
    receive_task = None
    reader_task = None
    wait_task = None
    try:
        first = await ws.receive_text()
        payload = json.loads(first)
        if payload.get("type") == "attach":
            terminal_session_id = str(
                payload.get("terminal_session_id")
                or ws.query_params.get("terminal_session_id")
                or ""
            ).strip()
            session = _TERMINAL_SESSIONS.get(terminal_session_id)
            if not session:
                raise ValueError("terminal session not found")
            await _attach_terminal_session(ws, session)
            return
        if payload.get("type") != "start":
            raise ValueError("first frame must be start")
        mode = payload.get("mode")
        argv = payload.get("argv") or []
        command = _resolve_terminal_command(mode, argv)
        terminal_cwd = _resolve_terminal_cwd(payload.get("cwd"))

        pid, fd = pty.fork()
        if pid == 0:
            if terminal_cwd is not None:
                os.chdir(str(terminal_cwd))
            os.execvp(command[0], command)

        _set_winsize(fd, int(payload.get("rows") or 24), int(payload.get("cols") or 80))
        await ws.send_text(json.dumps({"type": "ready"}))
        reader_task = asyncio.create_task(_pty_reader(ws, fd))
        wait_task = asyncio.create_task(_wait_process(pid))
        receive_task = asyncio.create_task(ws.receive())

        while True:
            done, _pending = await asyncio.wait({wait_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
            if wait_task in done:
                if reader_task:
                    reader_task.cancel()
                if receive_task:
                    receive_task.cancel()
                code = wait_task.result()
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(json.dumps({"type": "exit", "code": code}))
                return

            message = receive_task.result()
            receive_task = asyncio.create_task(ws.receive())
            if message.get("bytes") is not None:
                os.write(fd, message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            control = json.loads(text)
            if control.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif control.get("type") == "resize":
                _set_winsize(fd, int(control.get("rows") or 24), int(control.get("cols") or 80))
            elif control.get("type") == "signal":
                sig = signal.SIGINT if control.get("signal") == "SIGINT" else signal.SIGTERM
                os.kill(pid, sig)
            elif control.get("type") == "stdin_eof":
                # Keep the PTY open so read-only commands can continue emitting
                # output after the local stdin reaches EOF.
                continue
    except WebSocketDisconnect:
        return
    except Exception as exc:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await ws.close()
    finally:
        for task in (receive_task, reader_task):
            if task and not task.done():
                task.cancel()
        if pid is not None and (wait_task is None or not wait_task.done()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        if wait_task and not wait_task.done():
            wait_task.cancel()
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_dashboard(path: str, request: Request) -> Response:
    return await _proxy_http(request, _dashboard_base(), path)
