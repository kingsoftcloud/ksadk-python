"""Native terminal session manager for hosted Hermes/OpenClaw UIs."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import os
import pty
import select
import shutil
import signal
import termios
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

from ksadk.hermes_terminal import TERMINAL_SUBPROTOCOL
from ksadk.terminal_exec_policy import (
    GENERIC_TERMINAL_EXEC_POLICY,
    HERMES_TERMINAL_EXEC_POLICY,
    OPENCLAW_TERMINAL_EXEC_POLICY,
)
from ksadk.terminal_exec_policy import (
    validate_terminal_exec_argv as validate_exec_argv_with_policy,
)


TERMINAL_REPLAY_BUFFER_BYTES = 64 * 1024
TERMINAL_DETACHED_TTL_SECONDS = 24 * 60 * 60
MAX_TERMINAL_SESSIONS_PER_BUSINESS_SESSION = 3
MAX_TERMINAL_SESSIONS_PER_RUNTIME = 32


@dataclass
class TerminalSession:
    id: str
    session_id: str = ""
    mode: str = "tui"
    framework: str = ""
    argv: list[str] = field(default_factory=list)
    cols: int = 80
    rows: int = 24
    cwd: str = ""
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    pid: int | None = None
    fd: int | None = None
    exit_code: int | None = None
    deleted: bool = False
    transient: bool = False
    reader_task: asyncio.Task | None = None
    wait_task: asyncio.Task | None = None
    attachments: set[WebSocket] = field(default_factory=set)
    replay_buffer: bytearray = field(default_factory=bytearray)


def _utc_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class TerminalSessionManager:
    def __init__(
        self,
        *,
        workspace_root_getter: Callable[[], Path],
        framework_getter: Callable[[], str],
    ):
        self.workspace_root_getter = workspace_root_getter
        self.framework_getter = framework_getter
        self.sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    def reset_for_tests(self) -> None:
        for session in list(self.sessions.values()):
            self._terminate_session(session)
        self.sessions.clear()

    def serialize(self, session: TerminalSession) -> dict[str, Any]:
        return {
            "terminal_session_id": session.id,
            "session_id": session.session_id,
            "mode": session.mode,
            "status": session.status,
            "cols": session.cols,
            "rows": session.rows,
            "cwd": session.cwd,
            "created_at": _utc_timestamp(session.created_at),
            "updated_at": _utc_timestamp(session.updated_at),
            "exit_code": session.exit_code,
        }

    async def create_or_reuse(self, payload: dict[str, Any]) -> TerminalSession:
        session_id = str(
            payload.get("session_id") or payload.get("sessionId") or payload.get("SessionId") or ""
        ).strip()
        mode = str(payload.get("mode") or "tui").strip().lower()
        force_new = bool(payload.get("force_new") or payload.get("forceNew"))
        async with self._lock:
            self._cleanup_expired_locked()
            if not force_new:
                existing = self._find_reusable_locked(session_id=session_id, mode=mode)
                if existing:
                    existing.cols = int(payload.get("cols") or existing.cols or 80)
                    existing.rows = int(payload.get("rows") or existing.rows or 24)
                    existing.updated_at = time.time()
                    if existing.fd is not None:
                        _set_winsize(existing.fd, existing.rows, existing.cols)
                    return existing

            self._enforce_limits_locked(session_id=session_id)
            session = TerminalSession(
                id=f"term-{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                mode=mode,
                framework=self._current_framework(),
                argv=[str(item) for item in (payload.get("argv") or [])],
                cols=int(payload.get("cols") or 80),
                rows=int(payload.get("rows") or 24),
                cwd=str(payload.get("cwd") or "").strip(),
            )
            self._resolve_terminal_command(session)
            self.sessions[session.id] = session
            self._spawn_session(session)
            self._persist_metadata(session)
            return session

    async def list_sessions(self, *, session_id: str = "", mode: str = "") -> list[TerminalSession]:
        async with self._lock:
            self._cleanup_expired_locked()
            normalized_session_id = str(session_id or "").strip()
            normalized_mode = str(mode or "").strip().lower()
            sessions = [
                session
                for session in self.sessions.values()
                if not session.deleted
                and (not normalized_session_id or session.session_id == normalized_session_id)
                and (not normalized_mode or session.mode == normalized_mode)
            ]
            return sorted(
                sessions,
                key=lambda item: (
                    item.status in {"running", "detached"},
                    item.updated_at,
                ),
                reverse=True,
            )

    async def delete(self, terminal_session_id: str) -> TerminalSession | None:
        async with self._lock:
            session = self.sessions.get(terminal_session_id)
            if not session or session.deleted:
                return None
            session.deleted = True
            session.status = "deleted"
            session.updated_at = time.time()
            self._terminate_session(session)
            self._persist_metadata(session)
            return session

    async def attach(self, ws: WebSocket, terminal_session_id: str) -> None:
        session = self.sessions.get(terminal_session_id)
        if not session or session.deleted:
            raise ValueError("terminal session not found")
        await self._attach_existing(ws, session)

    async def legacy_start(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        session = TerminalSession(
            id=f"term-{uuid.uuid4().hex[:12]}",
            session_id=str(
                payload.get("session_id") or payload.get("sessionId") or payload.get("SessionId") or ""
            ).strip(),
            mode=str(payload.get("mode") or "tui").strip().lower(),
            framework=self._current_framework(),
            argv=[str(item) for item in (payload.get("argv") or [])],
            cols=int(payload.get("cols") or 80),
            rows=int(payload.get("rows") or 24),
            cwd=str(payload.get("cwd") or "").strip(),
            transient=True,
        )
        self._resolve_terminal_command(session)
        self.sessions[session.id] = session
        self._spawn_session(session)
        try:
            await self._attach_existing(ws, session)
        finally:
            self._terminate_session(session)
            self.sessions.pop(session.id, None)

    def _current_framework(self) -> str:
        return str(self.framework_getter() or "").strip().lower()

    def _find_reusable_locked(self, *, session_id: str, mode: str) -> TerminalSession | None:
        if not session_id:
            return None
        candidates = [
            session
            for session in self.sessions.values()
            if not session.deleted
            and session.session_id == session_id
            and session.mode == mode
            and session.status in {"running", "detached"}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.updated_at)

    def _cleanup_expired_locked(self) -> None:
        now = time.time()
        for session in list(self.sessions.values()):
            if session.deleted:
                continue
            if session.status == "detached" and now - session.updated_at > TERMINAL_DETACHED_TTL_SECONDS:
                session.deleted = True
                session.status = "deleted"
                self._terminate_session(session)

    def _enforce_limits_locked(self, *, session_id: str) -> None:
        active_sessions = [
            session
            for session in self.sessions.values()
            if not session.deleted and session.status in {"running", "detached"}
        ]
        if len(active_sessions) >= _env_int(
            "AGENTENGINE_TERMINAL_MAX_RUNTIME_SESSIONS",
            MAX_TERMINAL_SESSIONS_PER_RUNTIME,
        ):
            raise ValueError("too many terminal sessions in this runtime")
        if session_id:
            per_business_session = [session for session in active_sessions if session.session_id == session_id]
            if len(per_business_session) >= _env_int(
                "AGENTENGINE_TERMINAL_MAX_SESSIONS_PER_BUSINESS_SESSION",
                MAX_TERMINAL_SESSIONS_PER_BUSINESS_SESSION,
            ):
                raise ValueError("too many terminal sessions for this session_id")

    def _resolve_terminal_command(self, session: TerminalSession) -> list[str]:
        mode = session.mode
        framework = session.framework
        if mode == "tui":
            return self._resolve_tui_command(session)
        if mode == "exec":
            policy = HERMES_TERMINAL_EXEC_POLICY if framework == "hermes" else OPENCLAW_TERMINAL_EXEC_POLICY
            if framework not in {"hermes", "openclaw"}:
                policy = GENERIC_TERMINAL_EXEC_POLICY
            return validate_exec_argv_with_policy(session.argv, policy=policy)
        raise ValueError(f"unsupported terminal mode: {mode}")

    def _resolve_tui_command(self, session: TerminalSession) -> list[str]:
        framework = session.framework
        if framework == "hermes" and shutil.which("hermes"):
            command = ["hermes", "chat"]
            if session.session_id and _env_bool("HERMES_TERMINAL_RESUME_ENABLED", True):
                command.extend([os.getenv("HERMES_TERMINAL_RESUME_FLAG", "--resume"), session.session_id])
            session.argv = command
            return command
        if framework == "openclaw" and shutil.which("openclaw"):
            command = ["openclaw", "tui"]
            if session.session_id and _env_bool("OPENCLAW_TERMINAL_RESUME_ENABLED", True):
                command.extend([os.getenv("OPENCLAW_TERMINAL_RESUME_FLAG", "--resume"), session.session_id])
            session.argv = command
            return command
        shell = os.getenv("SHELL") or "/bin/sh"
        session.argv = [shell]
        return session.argv

    def _resolve_terminal_cwd(self, cwd: str | None) -> Path | None:
        raw = str(cwd or "").strip().replace("\\", "/")
        if not raw:
            return None

        workspace_root = self.workspace_root_getter().resolve()
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

    def _spawn_session(self, session: TerminalSession) -> None:
        if session.pid is not None:
            return
        command = self._resolve_terminal_command(session)
        terminal_cwd = self._resolve_terminal_cwd(session.cwd)
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
        session.reader_task = asyncio.create_task(self._session_reader(session))
        session.wait_task = asyncio.create_task(self._session_waiter(session))

    def _terminate_session(self, session: TerminalSession) -> None:
        if session.pid is not None and session.status not in {"exited", "deleted"}:
            with contextlib.suppress(ProcessLookupError):
                os.kill(session.pid, signal.SIGTERM)
        for task in (session.reader_task, session.wait_task):
            if task and not task.done():
                task.cancel()
        if session.fd is not None:
            with contextlib.suppress(OSError):
                os.close(session.fd)
            session.fd = None

    async def _session_reader(self, session: TerminalSession) -> None:
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
            await self._broadcast_bytes(session, data)

    async def _session_waiter(self, session: TerminalSession) -> None:
        if session.pid is None:
            return
        code = await _wait_process(session.pid)
        session.exit_code = code
        session.status = "exited"
        session.updated_at = time.time()
        await self._broadcast_control(session, {"type": "exit", "code": code})
        if session.fd is not None:
            with contextlib.suppress(OSError):
                os.close(session.fd)
            session.fd = None
        self._persist_metadata(session)

    async def _attach_existing(self, ws: WebSocket, session: TerminalSession) -> None:
        session.attachments.add(ws)
        session.status = "running" if session.status == "detached" and session.pid is not None else session.status
        session.updated_at = time.time()
        try:
            await ws.send_text(
                json.dumps({"type": "ready", "terminal_session_id": session.id}, ensure_ascii=False)
            )
            if session.replay_buffer:
                await ws.send_bytes(bytes(session.replay_buffer))
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    return
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
            if not session.attachments and session.status == "running":
                session.status = "detached"
                session.updated_at = time.time()
                self._persist_metadata(session)

    async def _broadcast_bytes(self, session: TerminalSession, data: bytes) -> None:
        session.replay_buffer.extend(data)
        overflow = len(session.replay_buffer) - _env_int(
            "AGENTENGINE_TERMINAL_REPLAY_BUFFER_BYTES",
            TERMINAL_REPLAY_BUFFER_BYTES,
        )
        if overflow > 0:
            del session.replay_buffer[:overflow]
        stale: list[WebSocket] = []
        for attached in list(session.attachments):
            try:
                await attached.send_bytes(data)
            except Exception:
                stale.append(attached)
        for attached in stale:
            session.attachments.discard(attached)

    async def _broadcast_control(self, session: TerminalSession, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        text = json.dumps(payload, ensure_ascii=False)
        for attached in list(session.attachments):
            try:
                await attached.send_text(text)
            except Exception:
                stale.append(attached)
        for attached in stale:
            session.attachments.discard(attached)

    def _persist_metadata(self, session: TerminalSession) -> None:
        try:
            state_dir = Path(
                os.getenv("AGENTENGINE_TERMINAL_STATE_DIR", "/home/node/.agentengine/terminal")
            )
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / f"{session.id}.json").write_text(
                json.dumps(self.serialize(session), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(Exception):
        termios.tcsetwinsize(fd, (int(rows or 24), int(cols or 80)))


async def _wait_process(pid: int) -> int:
    loop = asyncio.get_running_loop()
    _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return status


def register_terminal_routes(app: FastAPI, manager: TerminalSessionManager) -> None:
    @app.post("/_ksadk/terminal/sessions")
    async def create_terminal_session(request: Request) -> JSONResponse:
        payload = await request.json()
        try:
            session = await manager.create_or_reuse(payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"session": manager.serialize(session)})

    @app.get("/_ksadk/terminal/sessions")
    async def list_terminal_sessions(request: Request) -> JSONResponse:
        sessions = await manager.list_sessions(
            session_id=str(request.query_params.get("session_id") or ""),
            mode=str(request.query_params.get("mode") or ""),
        )
        return JSONResponse({"sessions": [manager.serialize(session) for session in sessions]})

    @app.delete("/_ksadk/terminal/sessions/{terminal_session_id}")
    async def delete_terminal_session(terminal_session_id: str) -> JSONResponse:
        session = await manager.delete(terminal_session_id)
        if not session:
            return JSONResponse(
                {"deleted": False, "terminal_session_id": terminal_session_id},
                status_code=404,
            )
        return JSONResponse({"deleted": True, "terminal_session_id": terminal_session_id})

    @app.websocket("/_ksadk/terminal/ws")
    async def terminal_ws(ws: WebSocket) -> None:
        if TERMINAL_SUBPROTOCOL not in (ws.headers.get("sec-websocket-protocol") or ""):
            await ws.close(code=4400, reason="missing ks-terminal.v1 subprotocol")
            return
        await ws.accept(subprotocol=TERMINAL_SUBPROTOCOL)
        try:
            query_session_id = str(ws.query_params.get("terminal_session_id") or "").strip()
            if query_session_id:
                await manager.attach(ws, query_session_id)
                return
            first = await ws.receive_text()
            payload = json.loads(first)
            if payload.get("type") == "attach":
                terminal_session_id = str(payload.get("terminal_session_id") or "").strip()
                await manager.attach(ws, terminal_session_id)
                return
            if payload.get("type") != "start":
                raise ValueError("first frame must be start")
            await manager.legacy_start(ws, payload)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
                await ws.close()
