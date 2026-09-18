"""Outbound WebSocket connector: local Agent ↔ cloud Channel Gateway.

Protocol: see docs/connector/channel-connector-protocol-v1.md.

Design constraints:
- Pure stdlib + ``websockets`` (already in ksadk runtime deps).
- No dependency on ``agentengine-channel`` Python package.
- Exponential backoff reconnect: 1s → 2s → 4s → … → 60s max.
- Task dedup by ``task_id`` (server redelivers on reconnect).
- Callback ``invoke(task_id, message) -> str`` is the only integration point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

InvokeFn = Callable[[str, str], Awaitable[str]]  # (task_id, message) -> result

_MAX_BACKOFF = 60.0
_BASE_BACKOFF = 1.0
_HEARTBEAT_GRACE = 5.0


class ChannelConnector:
    """Maintain an outbound WSS connection to the Channel Connector Gateway."""

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
    ) -> None:
        self._url = url
        self._token = token
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._invoke = invoke
        self._capabilities = capabilities or ["invoke", "stream", "cancel"]
        self._seq = 0
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._ws: Any = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _envelope(self, msg_type: str, payload: dict) -> str:
        return json.dumps({
            "type": msg_type,
            "seq": self._next_seq(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": payload,
        }, ensure_ascii=False)

    async def start(self) -> None:
        """Run the connector loop until ``stop()`` is called."""
        import websockets

        backoff = _BASE_BACKOFF
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    f"{self._url}?token={self._token}",
                    ping_interval=None,  # use protocol-level ping/pong
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    backoff = _BASE_BACKOFF
                    await self._register(ws)
                    await self._read_loop(ws)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("channel connector disconnected: %s; reconnecting in %.0fs", exc, backoff)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            finally:
                self._ws = None
                self._cancel_all_tasks()

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._cancel_all_tasks()

    def _cancel_all_tasks(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _register(self, ws: Any) -> None:
        await ws.send(self._envelope("register", {
            "tenant_id": self._tenant_id,
            "workspace_id": self._workspace_id,
            "agent_id": self._agent_id,
            "capabilities": self._capabilities,
            "protocol_version": 1,
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        if msg.get("type") == "register_err":
            raise ConnectionRefusedError(f"register rejected: {msg['payload'].get('message')}")
        if msg.get("type") != "register_ok":
            raise ProtocolError(f"expected register_ok, got {msg.get('type')}")
        logger.info("channel connector registered: session=%s", msg["payload"].get("session_id"))

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            msg_type = msg.get("type")
            payload = msg.get("payload", {})
            if msg_type == "invoke":
                self._spawn_invoke(payload)
            elif msg_type == "ping":
                await ws.send(self._envelope("pong", {}))
            elif msg_type == "cancel":
                task = self._tasks.pop(payload.get("task_id"), None)
                if task:
                    task.cancel()

    def _spawn_invoke(self, payload: dict) -> None:
        task_id = payload.get("task_id", "")
        message = payload.get("message", "")
        if task_id in self._tasks:
            return  # dedup
        self._tasks[task_id] = asyncio.ensure_future(self._execute(task_id, message))

    async def _execute(self, task_id: str, message: str) -> None:
        try:
            result = await asyncio.wait_for(
                self._invoke(task_id, message), timeout=300,
            )
            if self._ws:
                await self._ws.send(self._envelope("complete", {"task_id": task_id, "result": result}))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("channel invoke %s failed", task_id)
            if self._ws:
                await self._ws.send(self._envelope("error", {
                    "task_id": task_id, "code": "execution_error", "message": str(exc),
                }))
        finally:
            self._tasks.pop(task_id, None)


class ProtocolError(Exception):
    pass


def start_channel_connector(
    *,
    url: str,
    token: str,
    agent_id: str,
    tenant_id: str,
    workspace_id: str,
    invoke: InvokeFn,
    capabilities: list[str] | None = None,
) -> ChannelConnector:
    """Create and start a :class:`ChannelConnector`. Returns the instance;
    call ``await instance.start()`` to run the loop (or ``asyncio.create_task(instance.start())``).
    Call ``await instance.stop()`` to shut down."""
    conn = ChannelConnector(
        url=url, token=token, agent_id=agent_id,
        tenant_id=tenant_id, workspace_id=workspace_id,
        invoke=invoke, capabilities=capabilities,
    )
    return conn
