"""Outbound WebSocket connector for a local Agent and Channel Gateway."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)
_MAX_BACKOFF = 60.0
_BASE_BACKOFF = 1.0
_DEFAULT_TIMEOUT = 300.0
_TERMINAL_CACHE_SIZE = 1024
_STOP_TASK_TIMEOUT = 2.0
_BACKOFF_RESET_AFTER = 5.0


@dataclass(frozen=True)
class InvokeRequest:
    """The complete invocation context delivered by the Channel Gateway."""

    task_id: str
    session_id: str
    message: str
    channel: str = ""
    deadline: datetime | None = None
    idempotency_key: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InvokeRequest":
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ProtocolError("invoke requires task_id")
        deadline = payload.get("deadline")
        parsed_deadline: datetime | None = None
        if deadline:
            if not isinstance(deadline, str):
                raise ProtocolError("invoke deadline must be an ISO-8601 string")
            try:
                parsed_deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProtocolError("invoke deadline is not valid ISO-8601") from exc
            if parsed_deadline.tzinfo is None:
                parsed_deadline = parsed_deadline.replace(tzinfo=timezone.utc)
            else:
                parsed_deadline = parsed_deadline.astimezone(timezone.utc)
        return cls(
            task_id=task_id,
            session_id=str(payload.get("session_id") or ""),
            message=str(payload.get("message") or ""),
            channel=str(payload.get("channel") or ""),
            deadline=parsed_deadline,
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )

    def remaining_seconds(self, *, now: datetime | None = None) -> float | None:
        if self.deadline is None:
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (self.deadline - current.astimezone(timezone.utc)).total_seconds()


InvokeFn = Callable[[InvokeRequest], Awaitable[str]]


@dataclass(frozen=True)
class ConnectorStatus:
    """Safe-to-display lifecycle snapshot; it never contains a token."""

    state: str
    connected: bool
    active_tasks: int
    last_error: str | None


@dataclass(frozen=True)
class _Terminal:
    msg_type: str
    payload: dict[str, Any]


class ChannelConnector:
    """Maintain an outbound WSS connection and execute gateway invocations."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        agent_id: str,
        tenant_id: str,
        workspace_id: str,
        invoke: InvokeFn,
        capabilities: list[str] | None = None,
        default_timeout: float = _DEFAULT_TIMEOUT,
        completed_cache_size: int = _TERMINAL_CACHE_SIZE,
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        if completed_cache_size <= 0:
            raise ValueError("completed_cache_size must be positive")
        parts = urlsplit(url)
        try:
            loopback = (
                parts.hostname == "localhost"
                or ipaddress.ip_address(parts.hostname or "").is_loopback
            )
        except ValueError:
            loopback = False
        if (
            not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or any(ord(char) < 33 for char in url)
            or not (parts.scheme == "wss" or parts.scheme == "ws" and loopback)
        ):
            raise ValueError("connector requires WSS (loopback WS is allowed for development)")
        _ = parts.port
        self._url = url
        self._token = token
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._invoke = invoke
        self._capabilities = list(capabilities or ["invoke", "cancel"])
        self._default_timeout = default_timeout
        self._completed_cache_size = completed_cache_size
        self._seq = 0
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._requests: dict[str, InvokeRequest] = {}
        self._cancel_requested: set[str] = set()
        self._completed: OrderedDict[str, _Terminal] = OrderedDict()
        self._pending_terminal: set[str] = set()
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._ws: Any = None
        self._started = False
        self._state = "new"
        self._last_error: str | None = None

    @property
    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            state=self._state,
            connected=self._ws is not None and self._state == "connected",
            active_tasks=len(self._tasks),
            last_error=self._last_error,
        )

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _envelope(self, msg_type: str, payload: dict[str, Any]) -> str:
        return json.dumps(
            {
                "type": msg_type,
                "seq": self._next_seq(),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "payload": payload,
            },
            ensure_ascii=False,
        )

    def _connection_url(self) -> str:
        parts = urlsplit(self._url)
        query = [(key, value) for key, value in parse_qsl(parts.query) if key != "token"]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    async def start(self) -> None:
        """Run until :meth:`stop` is called, reconnecting with bounded backoff."""
        if self._started:
            raise RuntimeError("channel connector already started")
        self._started = True
        import websockets

        # websockets 14 renamed extra_headers when the asyncio client became
        # the default. Keep the supported 12-15 range without putting bearer
        # credentials into URLs that HTTP/WS access loggers commonly record.
        header_option = (
            "additional_headers"
            if "additional_headers" in inspect.signature(websockets.connect).parameters
            else "extra_headers"
        )
        connection_options = {header_option: {"Authorization": f"Bearer {self._token}"}}
        backoff = _BASE_BACKOFF
        self._state = "connecting"
        try:
            while not self._stop.is_set():
                try:
                    self._state = "connecting"
                    async with websockets.connect(
                        self._connection_url(),
                        ping_interval=None,
                        close_timeout=5,
                        **connection_options,
                    ) as ws:
                        self._ws = ws
                        self._state = "registering"
                        await self._register(ws)
                        self._state = "connected"
                        self._last_error = None
                        connected_at = time.monotonic()
                        await self._flush_pending_terminal(ws)
                        await self._read_loop(ws)
                        # A clean peer close is still a disconnect.  Turn it
                        # into the same bounded backoff path as a socket error
                        # instead of spinning a hot reconnect loop.
                        if time.monotonic() - connected_at >= _BACKOFF_RESET_AFTER:
                            backoff = _BASE_BACKOFF
                        if not self._stop.is_set():
                            raise ConnectionError("connection closed by peer")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._stop.is_set():
                        break
                    self._state = "backoff"
                    self._last_error = "connection_failed"
                    logger.warning("channel connector disconnected; retrying in %.0fs", backoff)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                finally:
                    self._ws = None
                    if not self._stop.is_set():
                        self._state = "connecting"
        finally:
            self._state = "stopped" if self._stop.is_set() else "disconnected"
            self._started = False

    async def stop(self) -> None:
        self._stop.set()
        self._state = "stopping"
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        await self._cancel_all_tasks()
        self._state = "stopped"

    async def _cancel_all_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=_STOP_TASK_TIMEOUT)
            # Retrieve ordinary exceptions and CancelledError without masking
            # cancellation of the caller of stop().
            await asyncio.gather(*done, return_exceptions=True)
            if pending:
                logger.warning("%d channel invoke task(s) did not cancel promptly", len(pending))
                for task in pending:
                    task.cancel()
                    task.add_done_callback(self._consume_task_result)
        # A non-cooperative callback may outlive the bounded shutdown wait; it
        # must not remain visible as an owned connector task or block shutdown.
        self._tasks.clear()
        self._requests.clear()
        self._cancel_requested.clear()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _send(self, ws: Any, msg_type: str, payload: dict[str, Any]) -> bool:
        if self._ws is not ws:
            return False
        frame = self._envelope(msg_type, payload)
        try:
            async with self._send_lock:
                if self._ws is not ws:
                    return False
                await ws.send(frame)
            return True
        except Exception:
            return False

    async def _register(self, ws: Any) -> None:
        await ws.send(
            self._envelope(
                "register",
                {
                    "tenant_id": self._tenant_id,
                    "workspace_id": self._workspace_id,
                    "agent_id": self._agent_id,
                    "capabilities": self._capabilities,
                    "protocol_version": 1,
                },
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        if msg.get("type") == "register_err":
            error_payload = msg.get("payload") or {}
            if not isinstance(error_payload, dict):
                error_payload = {}
            code = str(error_payload.get("code") or "registration_rejected")
            raise ConnectionRefusedError(code)
        if msg.get("type") != "register_ok":
            raise ProtocolError(f"expected register_ok, got {msg.get('type')}")
        payload = msg.get("payload") or {}
        if not isinstance(payload, dict):
            raise ProtocolError("register_ok payload must be an object")
        identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else payload
        expected_identity = (
            ("tenant_id", self._tenant_id),
            ("workspace_id", self._workspace_id),
            ("agent_id", self._agent_id),
        )
        for key, expected in expected_identity:
            returned = identity.get(key)
            if returned is not None and str(returned) != expected:
                raise ProtocolError(f"register identity mismatch: {key}")

    async def _flush_pending_terminal(self, ws: Any) -> None:
        for task_id in list(self._pending_terminal):
            terminal = self._completed.get(task_id)
            if terminal is None:
                self._pending_terminal.discard(task_id)
                continue
            if await self._send(ws, terminal.msg_type, terminal.payload):
                self._pending_terminal.discard(task_id)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ProtocolError("frame must be an object")
            msg_type = msg.get("type")
            payload = msg.get("payload") or {}
            if not isinstance(payload, dict):
                raise ProtocolError("payload must be an object")
            if msg_type == "invoke":
                await self._spawn_invoke(payload, ws)
            elif msg_type == "ping":
                await self._send(ws, "pong", {})
            elif msg_type == "cancel":
                await self._cancel_remote(str(payload.get("task_id") or ""))

    async def _spawn_invoke(self, payload: dict[str, Any], ws: Any | None = None) -> None:
        request = InvokeRequest.from_payload(payload)
        if request.task_id in self._completed:
            terminal = self._completed[request.task_id]
            await self._send(ws or self._ws, terminal.msg_type, terminal.payload)
            return
        if request.task_id in self._tasks:
            return
        self._requests[request.task_id] = request
        self._tasks[request.task_id] = asyncio.create_task(self._execute(request))

    async def _cancel_remote(self, task_id: str) -> None:
        if not task_id:
            return
        task = self._tasks.get(task_id)
        if task is not None:
            self._cancel_requested.add(task_id)
            task.cancel()

    async def _execute(self, request: InvokeRequest) -> None:
        task_id = request.task_id
        try:
            remaining = request.remaining_seconds()
            if remaining is not None and remaining <= 0:
                await self._finish(
                    task_id,
                    "error",
                    {
                        "task_id": task_id,
                        "code": "deadline_exceeded",
                        "message": "deadline exceeded",
                    },
                )
                return
            timeout = (
                self._default_timeout
                if remaining is None
                else min(self._default_timeout, remaining)
            )
            result = await asyncio.wait_for(self._invoke(request), timeout=timeout)
            await self._finish(task_id, "complete", {"task_id": task_id, "result": str(result)})
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                await self._finish(
                    task_id,
                    "error",
                    {"task_id": task_id, "code": "cancelled", "message": "task cancelled"},
                )
            raise
        except asyncio.TimeoutError:
            await self._finish(
                task_id,
                "error",
                {"task_id": task_id, "code": "deadline_exceeded", "message": "deadline exceeded"},
            )
        except Exception as exc:
            # Do not log exception text: Agent errors may contain credentials or
            # user content.  The wire error is intentionally generic as well.
            logger.warning("channel invoke failed (%s)", type(exc).__name__)
            await self._finish(
                task_id,
                "error",
                {
                    "task_id": task_id,
                    "code": "execution_error",
                    "message": "agent invocation failed",
                },
            )
        finally:
            self._tasks.pop(task_id, None)
            self._requests.pop(task_id, None)
            self._cancel_requested.discard(task_id)

    async def _finish(self, task_id: str, msg_type: str, payload: dict[str, Any]) -> None:
        self._completed[task_id] = _Terminal(msg_type, payload)
        self._completed.move_to_end(task_id)
        while len(self._completed) > self._completed_cache_size:
            old_task_id, _ = self._completed.popitem(last=False)
            self._pending_terminal.discard(old_task_id)
        ws = self._ws
        if ws is None or not await self._send(ws, msg_type, payload):
            self._pending_terminal.add(task_id)


class ProtocolError(Exception):
    """The peer sent a frame that violates the connector protocol."""


def create_channel_connector(
    *,
    url: str,
    token: str,
    agent_id: str,
    tenant_id: str,
    workspace_id: str,
    invoke: InvokeFn,
    capabilities: list[str] | None = None,
    default_timeout: float = _DEFAULT_TIMEOUT,
    completed_cache_size: int = _TERMINAL_CACHE_SIZE,
) -> ChannelConnector:
    """Create a connector; the host starts it with ``await connector.start()``."""
    return ChannelConnector(
        url=url,
        token=token,
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        invoke=invoke,
        capabilities=capabilities,
        default_timeout=default_timeout,
        completed_cache_size=completed_cache_size,
    )


__all__ = [
    "ChannelConnector",
    "ConnectorStatus",
    "InvokeRequest",
    "ProtocolError",
    "create_channel_connector",
]
