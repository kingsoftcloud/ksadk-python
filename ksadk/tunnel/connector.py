"""Enterprise-side outbound WebSocket connector for local MCP services."""

from __future__ import annotations

import asyncio
import codecs
import inspect
import json
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import websockets

logger = logging.getLogger(__name__)

_REGISTER_ACK_TIMEOUT_SECONDS = 10.0

_HOP_BY_HOP = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class LocalServer:
    """A local service whose connection secrets remain in the connector."""

    name: str
    address: str = field(repr=False)
    protocol: str = "mcp"
    tool_filter: list[str] | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    query: dict[str, str] = field(default_factory=dict, repr=False)

    def descriptor(self) -> dict[str, Any]:
        """Return only the public registration fields."""

        return {
            "name": self.name,
            "protocol": self.protocol,
            "tool_filter": self.tool_filter,
        }


def _local_request_url(server: LocalServer, message: dict[str, Any]) -> str:
    """Combine the local endpoint, forwarded path/query, and local query secrets."""

    parts = urlsplit(server.address)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("local server address must be an absolute HTTP(S) URL")

    forwarded_path = message.get("path", "")
    if not isinstance(forwarded_path, str):
        raise ValueError("forwarded path must be a string")
    path = parts.path
    if forwarded_path:
        path = f"{parts.path.rstrip('/')}/{forwarded_path.lstrip('/')}"

    forwarded_query = message.get("query", "")
    if not isinstance(forwarded_query, str):
        raise ValueError("forwarded query must be a string")
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_items.extend(parse_qsl(forwarded_query, keep_blank_values=True))
    if server.query:
        local_keys = set(server.query)
        query_items = [(key, value) for key, value in query_items if key not in local_keys]
        query_items.extend(server.query.items())

    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query_items, doseq=True), ""))


class TunnelConnector:
    """Register local services and concurrently bridge forwarded HTTP requests."""

    def __init__(
        self,
        cloud_url: str,
        agent: str,
        servers: list[LocalServer],
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.cloud_url = cloud_url
        self.agent = agent
        self.servers: dict[str, LocalServer] = {}
        for server in servers:
            if server.name in self.servers:
                raise ValueError(f"duplicate local server name: {server.name}")
            self.servers[server.name] = server
        self.token = token
        self.extra_headers = dict(extra_headers or {})
        self._token_via_query = bool(token) and any(
            key.lower() == "authorization" for key in self.extra_headers
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._send_lock = asyncio.Lock()

    def _ws_url(self) -> str:
        """Build the connector endpoint without string-replacing URL contents."""

        parts = urlsplit(self.cloud_url)
        schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
        if parts.scheme not in schemes or not parts.netloc:
            raise ValueError("cloud_url must be an absolute HTTP(S) or WS(S) URL")
        query = parse_qsl(parts.query, keep_blank_values=True)
        if self._token_via_query and self.token is not None:
            query = [(key, value) for key, value in query if key != "token"]
            query.append(("token", self.token))
        path = f"{parts.path.rstrip('/')}/tunnel/connect"
        return urlunsplit((schemes[parts.scheme], parts.netloc, path, urlencode(query), ""))

    def _handshake_headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.token and not self._token_via_query:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _register_message(self) -> dict[str, Any]:
        return {
            "type": "register",
            "agent": self.agent,
            "servers": [server.descriptor() for server in self.servers.values()],
        }

    @staticmethod
    def _header_kwargs(headers: dict[str, str]) -> dict[str, dict[str, str]]:
        """Select the handshake header argument used by websockets 12 through 15."""

        parameters = inspect.signature(websockets.connect).parameters
        if "additional_headers" in parameters:
            return {"additional_headers": headers}
        if "extra_headers" in parameters:
            return {"extra_headers": headers}
        raise RuntimeError("installed websockets client does not support handshake headers")

    async def _send_json(self, websocket: Any, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await websocket.send(json.dumps(message))

    async def start(self) -> None:
        """Run until disconnected, always cancelling and reaping local HTTP tasks."""

        try:
            async with httpx.AsyncClient(timeout=None) as http:
                headers = self._header_kwargs(self._handshake_headers())
                async with websockets.connect(self._ws_url(), **headers) as websocket:
                    await self._send_json(websocket, self._register_message())
                    ack = json.loads(
                        await asyncio.wait_for(
                            websocket.recv(),
                            timeout=_REGISTER_ACK_TIMEOUT_SECONDS,
                        )
                    )
                    if not isinstance(ack, dict) or not ack.get("ok"):
                        error = ack.get("error") if isinstance(ack, dict) else None
                        detail = error or "unknown error"
                        raise RuntimeError(f"tunnel registration rejected: {detail}")
                    logger.info(
                        "Tunnel connected for agent %s with servers %s (connector %s)",
                        self.agent,
                        list(self.servers),
                        ack.get("connector_id"),
                    )

                    async for raw in websocket:
                        message = json.loads(raw)
                        if not isinstance(message, dict):
                            raise ValueError("tunnel frame must be a JSON object")
                        frame_type = message.get("type")
                        if frame_type == "http_request":
                            self._start_request(websocket, http, message)
                        elif frame_type == "http_cancel":
                            self._cancel_request(message.get("id"))
                        else:
                            logger.debug("Ignoring unsupported tunnel frame type")
        finally:
            await self._cancel_all_tasks()

    def _start_request(
        self,
        websocket: Any,
        http: httpx.AsyncClient,
        message: dict[str, Any],
    ) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("http_request requires a non-empty id")
        if request_id in self._tasks:
            logger.warning("Ignoring duplicate tunnel request id %s", request_id)
            return
        task = asyncio.create_task(self._handle_request(websocket, http, message))
        self._tasks[request_id] = task
        task.add_done_callback(partial(self._task_done, request_id))

    def _task_done(self, request_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(request_id) is task:
            self._tasks.pop(request_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.debug("Tunnel request %s task failed after cleanup", request_id)

    def _cancel_request(self, request_id: Any) -> None:
        if not isinstance(request_id, str):
            return
        task = self._tasks.get(request_id)
        if task is not None:
            task.cancel()

    async def _cancel_all_tasks(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _handle_request(
        self,
        websocket: Any,
        http: httpx.AsyncClient,
        message: dict[str, Any],
    ) -> None:
        request_id = message["id"]
        server = self.servers.get(message.get("server", ""))
        if server is None:
            await self._send_json(
                websocket,
                {"type": "http_error", "id": request_id, "error": "unknown server"},
            )
            return

        try:
            forwarded_headers = {
                key: value
                for key, value in (message.get("headers") or {}).items()
                if key.lower() not in _HOP_BY_HOP
            }
            for key, value in server.headers.items():
                for existing in list(forwarded_headers):
                    if existing.lower() == key.lower():
                        forwarded_headers.pop(existing, None)
                forwarded_headers[key] = value

            body = message.get("body", "")
            if not isinstance(body, str):
                raise ValueError("forwarded body must be text")
            async with http.stream(
                method=message.get("method", "POST"),
                url=_local_request_url(server, message),
                headers=forwarded_headers,
                content=body.encode("utf-8"),
            ) as response:
                await self._send_json(
                    websocket,
                    {
                        "type": "http_response",
                        "id": request_id,
                        "status": response.status_code,
                        "headers": dict(response.headers),
                    },
                )
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                async for chunk in response.aiter_bytes():
                    data = decoder.decode(chunk)
                    if data:
                        await self._send_json(
                            websocket,
                            {"type": "http_chunk", "id": request_id, "data": data},
                        )
                final_data = decoder.decode(b"", final=True)
                if final_data:
                    await self._send_json(
                        websocket,
                        {
                            "type": "http_chunk",
                            "id": request_id,
                            "data": final_data,
                        },
                    )
                await self._send_json(websocket, {"type": "http_end", "id": request_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Local tunnel request %s for server %s failed (%s)",
                request_id,
                server.name,
                type(exc).__name__,
            )
            try:
                await self._send_json(
                    websocket,
                    {
                        "type": "http_error",
                        "id": request_id,
                        "error": f"local request failed ({type(exc).__name__})",
                    },
                )
            except Exception:
                logger.debug("Could not report local request failure for %s", request_id)


__all__ = ["LocalServer", "TunnelConnector"]
