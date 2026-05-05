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
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketState


TERMINAL_SUBPROTOCOL = "ks-terminal.v1"
SHELL_METACHARS = set("|&;<>()$`\\\n\r")

app = FastAPI()


@dataclass
class TerminalSession:
    id: str
    mode: str = "tui"
    argv: list[str] = field(default_factory=list)
    cols: int = 80
    rows: int = 24
    session_id: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    status: str = "starting"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pid: int | None = None
    fd: int | None = None
    exit_code: int | None = None
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
        "session_id": session.session_id,
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
    text = json.dumps(payload, ensure_ascii=False)
    stale: list[WebSocket] = []
    for attached in list(session.attachments):
        try:
            await attached.send_text(text)
        except Exception:
            stale.append(attached)
    for attached in stale:
        session.attachments.discard(attached)


def _gateway_base() -> str:
    configured = str(os.getenv("OPENCLAW_GATEWAY_PROXY_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    host = os.getenv("OPENCLAW_GATEWAY_INTERNAL_HOST", "127.0.0.1")
    port = os.getenv("OPENCLAW_GATEWAY_INTERNAL_PORT", "18080")
    return f"http://{host}:{port}"


def _gateway_ws_url() -> str:
    configured = str(os.getenv("OPENCLAW_GATEWAY_PROXY_WS_URL") or "").strip()
    if configured:
        return configured
    base = _gateway_base()
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/"
    return base


def _gateway_ws_target(path: str, query: str = "") -> str:
    base = _gateway_ws_url()
    parsed = urlsplit(base)
    base_path = parsed.path.rstrip("/")
    target_path = f"{base_path}/{path.lstrip('/')}" if path else (base_path or "/")
    return urlunsplit((parsed.scheme, parsed.netloc, target_path, query, ""))


def _shared_gateway_secret() -> tuple[str, str] | None:
    token = str(os.getenv("OPENCLAW_GATEWAY_TOKEN") or "").strip()
    if token:
        return "token", token
    password = str(os.getenv("OPENCLAW_GATEWAY_PASSWORD") or "").strip()
    if password:
        return "password", password
    return None


def _extract_bearer_token(headers: Mapping[str, str]) -> str:
    auth = str(headers.get("authorization") or headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth[7:].strip()


def _require_terminal_auth(ws: WebSocket) -> None:
    secret = _shared_gateway_secret()
    if not secret:
        return
    _secret_kind, secret_value = secret
    if _extract_bearer_token(ws.headers) == secret_value:
        return
    raise PermissionError(
        "OPENCLAW_GATEWAY_TOKEN/OPENCLAW_GATEWAY_PASSWORD required for OpenClaw terminal websocket"
    )


def _authorized_by_shared_gateway_secret(headers: Mapping[str, str]) -> bool:
    secret = _shared_gateway_secret()
    if not secret:
        return False
    _secret_kind, secret_value = secret
    return _extract_bearer_token(headers) == secret_value


def _attach_token_mode_operator_scope(headers: dict[str, str]) -> None:
    if not _authorized_by_shared_gateway_secret(headers):
        return
    for key in list(headers.keys()):
        if key.lower() == "x-openclaw-scopes":
            headers.pop(key, None)
    # OpenClaw 2026.3.28 still requires an explicit scope header for the
    # OpenResponses HTTP API. Token/password mode is already full-operator auth,
    # so the proxy normalizes that upstream version difference here.
    headers["x-openclaw-scopes"] = "operator.write"


def _validate_empty_argv(argv: Iterable[str]) -> None:
    normalized = [str(item).strip() for item in argv]
    if normalized:
        raise ValueError("OpenClaw native TUI does not accept argv")
    for item in normalized:
        if not item or any(char in SHELL_METACHARS for char in item):
            raise ValueError(f"unsafe argv: {item}")


def _append_text_option(command: list[str], flag: str, value: Any, *, max_length: int = 10000) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    if "\x00" in text or len(text) > max_length:
        raise ValueError(f"unsafe OpenClaw tui option: {flag}")
    command.extend([flag, text])


def _append_token_option(command: list[str], flag: str, value: Any) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text:
        return
    if any(char in SHELL_METACHARS for char in text):
        raise ValueError(f"unsafe OpenClaw tui option: {flag}")
    command.extend([flag, text])


def _append_int_option(
    command: list[str],
    flag: str,
    value: Any,
    *,
    minimum: int = 1,
    maximum: int = 86_400_000,
) -> None:
    if value is None:
        return
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid OpenClaw tui option: {flag}") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"invalid OpenClaw tui option: {flag}")
    command.extend([flag, str(number)])


def _append_tui_options(command: list[str], options: Mapping[str, Any] | None) -> None:
    if not options:
        return
    allowed = {"message", "thinking", "history_limit", "timeout_ms", "deliver"}
    unknown = sorted(str(key) for key in options.keys() if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unsupported OpenClaw tui options: {', '.join(unknown)}")

    _append_text_option(command, "--message", options.get("message"))
    _append_token_option(command, "--thinking", options.get("thinking"))
    _append_int_option(command, "--history-limit", options.get("history_limit"), maximum=10000)
    _append_int_option(command, "--timeout-ms", options.get("timeout_ms"))
    if bool(options.get("deliver")):
        command.append("--deliver")


def _resolve_terminal_command(
    mode: str,
    argv: Iterable[str],
    *,
    session_id: str | None,
    options: Mapping[str, Any] | None = None,
) -> list[str]:
    normalized_mode = str(mode or "").strip().lower()
    _validate_empty_argv(argv)
    if normalized_mode != "tui":
        raise ValueError("OpenClaw runtime proxy only supports tui mode")

    command = ["openclaw", "tui", "--url", _gateway_ws_url()]
    secret = _shared_gateway_secret()
    if secret:
        secret_kind, secret_value = secret
        command.extend(["--token" if secret_kind == "token" else "--password", secret_value])
    normalized_session = str(session_id or "").strip()
    if normalized_session:
        command.extend(["--session", normalized_session])
    _append_tui_options(command, options)
    return command


async def _proxy_http(request: Request, base_url: str, path: str) -> Response:
    target = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    _attach_token_mode_operator_scope(headers)
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
    return Response(body, status_code=upstream.status_code, headers=response_headers)


def _websocket_proxy_headers(ws: WebSocket) -> dict[str, str]:
    hop_by_hop = {
        "host",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    }
    return {
        key: value
        for key, value in ws.headers.items()
        if key.lower() not in hop_by_hop
    }


async def _connect_gateway_websocket(target: str, headers: dict[str, str]):
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - image dependency guard
        raise RuntimeError("missing dependency websockets for OpenClaw websocket proxy") from exc

    try:
        return websockets.connect(target, additional_headers=headers or None, max_size=None)
    except TypeError:  # websockets < 14
        return websockets.connect(target, extra_headers=headers or None, max_size=None)


async def _proxy_websocket(ws: WebSocket, path: str) -> None:
    target = _gateway_ws_target(path, ws.url.query)
    await ws.accept()
    connection = await _connect_gateway_websocket(target, _websocket_proxy_headers(ws))
    async with connection as upstream:
        async def _client_to_upstream() -> None:
            while True:
                message = await ws.receive()
                if message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                    continue
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                    continue
                if message.get("type") == "websocket.disconnect":
                    await upstream.close()
                    return

        async def _upstream_to_client() -> None:
            async for message in upstream:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(str(message))

        client_task = asyncio.create_task(_client_to_upstream())
        upstream_task = asyncio.create_task(_upstream_to_client())
        done, pending = await asyncio.wait(
            {client_task, upstream_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()


async def _health_check() -> dict:
    url = f"{_gateway_base()}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    return {"ok": 200 <= int(response.status_code) < 500, "url": url, "status_code": int(response.status_code)}


@app.get("/_ksadk/runtime-proxy/health")
async def runtime_proxy_health() -> JSONResponse:
    gateway = await _health_check()
    return JSONResponse({"ok": bool(gateway.get("ok")), "gateway": gateway})


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
    command = _resolve_terminal_command(
        session.mode,
        session.argv,
        session_id=session.session_id,
        options=session.options,
    )
    pid, fd = pty.fork()
    if pid == 0:
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
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("OpenClaw terminal options must be an object")
    terminal_session_id = f"term-{uuid.uuid4().hex[:12]}"
    session_id = str(payload.get("session_id") or terminal_session_id).strip()
    _resolve_terminal_command(mode, argv, session_id=session_id, options=options)
    session = TerminalSession(
        id=terminal_session_id,
        mode=mode,
        argv=argv,
        cols=int(payload.get("cols") or 80),
        rows=int(payload.get("rows") or 24),
        session_id=session_id,
        options=options,
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
        while True:
            message = await ws.receive()
            if message.get("bytes") is not None:
                if session.fd is not None and session.status == "running":
                    os.write(session.fd, message["bytes"])
                continue
            text = message.get("text")
            if not text:
                continue
            control = json.loads(text)
            if control.get("type") == "resize":
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
        _require_terminal_auth(ws)
        session_id = ws.headers.get("x-session-id")
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError("OpenClaw terminal options must be an object")
        command = _resolve_terminal_command(
            payload.get("mode"),
            payload.get("argv") or [],
            session_id=session_id,
            options=options,
        )

        pid, fd = pty.fork()
        if pid == 0:
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
            if control.get("type") == "resize":
                _set_winsize(fd, int(control.get("rows") or 24), int(control.get("cols") or 80))
            elif control.get("type") == "signal":
                sig = signal.SIGINT if control.get("signal") == "SIGINT" else signal.SIGTERM
                os.kill(pid, sig)
            elif control.get("type") == "stdin_eof":
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


@app.websocket("/{path:path}")
async def proxy_gateway_websocket(path: str, ws: WebSocket) -> None:
    try:
        await _proxy_websocket(ws, path)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.close(code=1011, reason=str(exc)[:120])


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_gateway(path: str, request: Request) -> Response:
    return await _proxy_http(request, _gateway_base(), path)
