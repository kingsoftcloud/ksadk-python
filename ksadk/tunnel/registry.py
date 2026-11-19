"""App-owned registrations and request correlation, never a process global registry."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

# The transport, response streams and MCP client must use the same backend,
# even when both httpx packages are installed in a legacy MCP environment.
from ksadk._mcp_compat import httpx_module

httpx = httpx_module()

from .protocol import LOCAL_ERROR_CODES, TunnelError, TunnelLimits, decode_request_body, encode


@dataclass(eq=False)
class ConnectorConnection:
    agent_id: str
    services: dict[str, dict]
    websocket: Any = field(repr=False)
    limits: TunnelLimits
    connector_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    pending: dict[str, asyncio.Queue] = field(default_factory=dict, repr=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    operations: set[asyncio.Timeout] = field(default_factory=set, repr=False)
    closed: bool = False
    owner: dict | None = None

    async def send(self, frame: dict) -> None:
        raw = encode(frame, self.limits)
        async with asyncio.timeout(self.limits.send_timeout):
            async with self._send_lock:
                await self.websocket.send_text(raw)

    async def forward(self, service: str, request: httpx.Request) -> httpx.Response:
        body = decode_request_body(await request.aread())
        if self.closed:
            raise TunnelError("disconnected")
        if len(self.pending) >= 3 * self.limits.max_inflight:
            raise TunnelError("busy")
        request_id = uuid.uuid4().hex
        queue = asyncio.Queue(maxsize=32)
        self.pending[request_id] = queue
        stream = ResponseStream(self, request_id, queue)
        try:
            await self.send(
                {
                    "type": "http_request",
                    "id": request_id,
                    "server": service,
                    "method": request.method,
                    "path": request.url.path.strip("/"),
                    "query": request.url.query.decode("ascii"),
                    "headers": {
                        k: v
                        for k, v in request.headers.items()
                        if k.lower() not in {"host", "content-length", "accept-encoding"}
                    },
                    "body": body,
                }
            )
            head = await stream.receive()
            if head["type"] != "http_response":
                raise TunnelError("invalid_result")
            return httpx.Response(head["status"], headers=head["headers"], stream=stream)
        except BaseException:
            await stream.aclose()
            raise

    def dispatch(self, frame: dict) -> None:
        if frame.get("type") not in {"http_response", "http_chunk", "http_end", "http_error"}:
            raise ValueError("unexpected HTTP tunnel frame")
        frame_type = frame["type"]
        if frame_type == "http_response":
            status, headers = frame.get("status"), frame.get("headers")
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, dict):
                raise ValueError("invalid HTTP response head")
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                raise ValueError("invalid HTTP response headers")
        elif frame_type == "http_chunk" and not isinstance(frame.get("data"), str):
            raise ValueError("invalid HTTP response chunk")
        queue = self.pending.get(frame["id"])
        if queue is not None:
            if queue.full():
                # Never let one slow response block the shared WS reader.
                self.pending.pop(frame["id"])
                queue.get_nowait()
                queue.put_nowait({"type": "http_error"})
            else:
                queue.put_nowait(frame)

    def fail_all(self) -> None:
        self.closed = True
        # Wake MCP sessions too, including a caller waiting on a parsed SSE event.
        for deadline in self.operations:
            if not deadline.expired():
                deadline.reschedule(asyncio.get_running_loop().time())
        for queue in self.pending.values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"type": "http_error"})
        self.pending.clear()


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, connection, request_id, queue):
        self.connection = connection
        self.request_id = request_id
        self.queue = queue
        self.done = False

    async def receive(self):
        async with asyncio.timeout(self.connection.limits.request_timeout):
            frame = await self.queue.get()
        if frame["type"] == "http_error":
            code = frame.get("error")
            raise TunnelError(
                code
                if isinstance(code, str) and code in LOCAL_ERROR_CODES
                else "local_service_error"
            )
        return frame

    async def __aiter__(self):
        received = 0
        try:
            while True:
                frame = await self.receive()
                if frame["type"] == "http_end":
                    self.done = True
                    return
                if frame["type"] != "http_chunk":
                    raise TunnelError("invalid_result")
                data = frame["data"].encode("utf-8")
                received += len(data)
                if received > self.connection.limits.max_response_bytes:
                    raise TunnelError("frame_too_large")
                yield data
        finally:
            await self.aclose()

    async def aclose(self):
        if self.queue is None:
            return
        self.queue = None
        self.connection.pending.pop(self.request_id, None)
        if not self.done and not self.connection.closed:
            try:
                await self.connection.send({"type": "http_cancel", "id": self.request_id})
            except Exception:
                pass


class TunnelRegistry:
    """One registry per runtime app; duplicates never replace a live connector."""

    def __init__(self):
        self._connections: dict[tuple[str, str], ConnectorConnection] = {}
        self._closed = False
        self.peers = None

    def add(self, connection: ConnectorConnection) -> None:
        if self._closed:
            raise TunnelError("closed")
        if any(
            (connection.agent_id, service) in self._connections for service in connection.services
        ):
            raise TunnelError("duplicate_service")
        for service in connection.services:
            self._connections[connection.agent_id, service] = connection
        if self.peers is not None:
            connection.owner = self.peers.owner

    def remove(self, connection: ConnectorConnection) -> None:
        connection.fail_all()
        for service in connection.services:
            key = (connection.agent_id, service)
            if self._connections.get(key) is connection:
                self._connections.pop(key)

    def connections(self, agent_id: str) -> list[tuple[str, ConnectorConnection]]:
        return [
            (service, conn)
            for (agent, service), conn in self._connections.items()
            if agent == agent_id and not conn.closed
        ]

    async def discover_connections(self, agent_id):
        if self.peers is None or not await self.peers.start():
            return self.connections(agent_id)
        local = self.connections(agent_id)
        for _, connection in local:
            connection.owner = self.peers.owner
        connections = [*local, *await self.peers.discover(agent_id, has_local=bool(local))]
        aliases = {}
        for service, connection in connections:
            identity = (connection.connector_id, json_owner(connection.owner))
            if service in aliases and aliases[service] != identity:
                raise TunnelError("ambiguous_service")
            aliases[service] = identity
        return connections

    async def list_tools(self, agent_id, service, *, connector_id=None, owner=None):
        return await self._operate(agent_id, service, connector_id, None, None, owner)

    async def call_tool(self, agent_id, service, name, arguments, *, connector_id=None, owner=None):
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise TunnelError("invalid_request")
        return await self._operate(agent_id, service, connector_id, name, arguments, owner)

    async def _operate(self, agent_id, service, connector_id, name, arguments, owner=None):
        from .http_tools import TunnelTransport, api_operation, mcp_operation

        conn = self._connections.get((agent_id, service))
        if self.peers is not None:
            if owner is not None and owner != self.peers.owner:
                conn = await self.peers.resolve(agent_id, service, connector_id, owner)
                if conn.owner != owner:
                    raise TunnelError("disconnected")
            elif owner is None and (
                conn is None or (connector_id is not None and conn.connector_id != connector_id)
            ):
                connections = await self.discover_connections(agent_id)
                conn = next(
                    (
                        candidate
                        for alias, candidate in connections
                        if alias == service
                        and (connector_id is None or candidate.connector_id == connector_id)
                    ),
                    None,
                )
        if conn is None or (connector_id is not None and connector_id != conn.connector_id):
            raise TunnelError("disconnected")
        if len(conn.operations) >= conn.limits.max_inflight:
            raise TunnelError("busy")
        deadline = asyncio.timeout(conn.limits.request_timeout)
        peer_operation = None
        try:
            descriptor = conn.services[service]
            operation = mcp_operation if descriptor["protocol"] == "mcp" else api_operation
            async with deadline:
                conn.operations.add(deadline)
                scope = (
                    self.peers.operation_scope(deadline)
                    if self.peers is not None
                    else nullcontext(None)
                )
                with scope as peer_operation:
                    return await operation(
                        TunnelTransport(conn, service), descriptor, name, arguments
                    )
        except TunnelError as exc:
            if exc.code in {
                "local_mcp_error",
                "local_service_error",
                "timeout",
                "disconnected",
                "frame_too_large",
            }:
                raise TunnelError(exc.code, indeterminate=name is not None) from None
            raise
        except TimeoutError:
            code = (
                peer_operation.error
                if peer_operation is not None and peer_operation.error
                else "disconnected" if conn.closed else "timeout"
            )
            raise TunnelError(
                code,
                indeterminate=name is not None
                and code not in {"busy", "forbidden", "invalid_request"},
            ) from None
        except Exception:
            code = (
                peer_operation.error
                if peer_operation is not None and peer_operation.error
                else "local_service_error"
            )
            raise TunnelError(
                code,
                indeterminate=name is not None
                and code not in {"busy", "forbidden", "invalid_request"},
            ) from None
        finally:
            conn.operations.discard(deadline)

    async def close(self) -> None:
        self._closed = True
        connections = set(self._connections.values())
        self._connections.clear()
        if self.peers is not None:
            await self.peers.close()
        for conn in connections:
            conn.fail_all()
        await asyncio.gather(
            *(conn.websocket.close(code=1001) for conn in connections), return_exceptions=True
        )


def json_owner(owner):
    return (owner["address"], owner["instance_id"]) if owner is not None else None
