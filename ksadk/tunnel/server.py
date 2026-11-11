"""FastAPI routes for authenticated connector registration and MCP proxying."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from ksadk.tunnel.registry import (
    ServerDescriptor,
    TunnelRegistry,
    get_registry,
    validate_registration,
)

logger = logging.getLogger(__name__)

_REGISTER_TIMEOUT_SECONDS = 10.0
_HEAD_TIMEOUT_SECONDS = 30.0
_CANCEL_TIMEOUT_SECONDS = 1.0

_HOP_BY_HOP = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP_BY_HOP}


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


def _secrets_equal(presented: str, expected: str) -> bool:
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class TunnelConnectionError(RuntimeError):
    """A request could not be sent through an active connector."""


class TunnelStreamError(RuntimeError):
    """A streaming response failed after its HTTP headers were sent."""


class _RequestQueue(asyncio.Queue):
    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id


@dataclass
class ConnectorConnection:
    """One registered WebSocket and the requests currently streaming through it."""

    connector_id: str
    websocket: WebSocket
    agent_name: str
    servers: list[ServerDescriptor]
    pending: dict[str, _RequestQueue] = field(default_factory=dict)
    active: bool = True
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def _send_json(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(message))

    async def request(self, server: str, payload: dict[str, Any]) -> _RequestQueue:
        """Send one request and return its ID-bearing response queue."""

        if not self.active:
            raise TunnelConnectionError("connector disconnected")
        request_id = uuid.uuid4().hex
        queue = _RequestQueue(request_id)
        self.pending[request_id] = queue
        try:
            await self._send_json(
                {
                    "type": "http_request",
                    "id": request_id,
                    "server": server,
                    **payload,
                }
            )
        except asyncio.CancelledError:
            if self.pending.get(request_id) is queue:
                self.pending.pop(request_id, None)
            await asyncio.shield(self._send_cancel_bounded(request_id))
            raise
        except Exception as exc:
            if self.pending.get(request_id) is queue:
                self.pending.pop(request_id, None)
            raise TunnelConnectionError("could not send tunnel request") from exc
        return queue

    async def _send_cancel_bounded(self, request_id: str) -> None:
        """Best-effort cancellation with a deadline even under write backpressure."""

        try:
            await asyncio.wait_for(
                self._send_json({"type": "http_cancel", "id": request_id}),
                timeout=_CANCEL_TIMEOUT_SECONDS,
            )
        except (Exception, asyncio.CancelledError):
            logger.debug("Could not cancel request %s after send interruption", request_id)

    def dispatch(self, message: dict[str, Any]) -> None:
        """Dispatch a response frame, dropping valid frames for expired requests."""

        if not isinstance(message, dict):
            raise ValueError("tunnel frame must be an object")
        request_id = message.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("response frame requires a request id")
        queue = self.pending.get(request_id)
        if queue is None:
            logger.debug("Dropping late frame for request %s", request_id)
            return

        frame_type = message.get("type")
        if frame_type == "http_response":
            status = message.get("status")
            headers = message.get("headers", {})
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or not 100 <= status <= 599
                or not isinstance(headers, dict)
            ):
                self._finish(queue, ("error", "invalid response head"))
            else:
                queue.put_nowait(("head", status, headers))
        elif frame_type == "http_chunk":
            data = message.get("data")
            if not isinstance(data, str):
                self._finish(queue, ("error", "invalid response chunk"))
            else:
                queue.put_nowait(("chunk", data))
        elif frame_type == "http_end":
            self._finish(queue, ("end",))
        elif frame_type == "http_error":
            self._finish(queue, ("error", "connector request failed"))
        else:
            raise ValueError("unsupported response frame type")

    def _finish(self, queue: _RequestQueue, event: tuple[Any, ...]) -> None:
        if self.pending.get(queue.request_id) is queue:
            self.pending.pop(queue.request_id, None)
            queue.put_nowait(event)

    async def cancel(self, request_id: str) -> None:
        """Forget a pending request and best-effort cancel its connector task."""

        queue = self.pending.pop(request_id, None)
        if queue is None:
            return
        await self._send_cancel_bounded(request_id)

    def fail_all(self) -> None:
        """Fail every waiter exactly once when the connector disconnects."""

        self.active = False
        pending = list(self.pending.values())
        self.pending.clear()
        for queue in pending:
            queue.put_nowait(("error", "connector disconnected"))


AuthFn = Callable[[str | None, str], bool | Awaitable[bool]]


def mount_tunnel(
    app: FastAPI,
    *,
    token: str | None = None,
    allowed_agents: list[str] | None = None,
    auth: AuthFn | None = None,
    registry: TunnelRegistry | None = None,
) -> None:
    """Mount Tunnel routes using an injected or embedded-fallback registry."""

    if auth is None and (not isinstance(token, str) or not token.strip()):
        raise ValueError("mount_tunnel requires a non-empty token or custom auth")
    reg = registry if registry is not None else get_registry()
    agents = frozenset(allowed_agents) if allowed_agents is not None else None

    async def _authorize(presented: str | None, agent_name: str) -> bool:
        if auth is not None:
            try:
                result = auth(presented, agent_name)
                if inspect.isawaitable(result):
                    result = await result
                return bool(result)
            except Exception as exc:
                logger.warning("Custom tunnel auth failed (%s)", type(exc).__name__)
                return False
        if token is None or presented is None or not _secrets_equal(presented, token):
            return False
        return agents is None or agent_name in agents

    def _request_tunnel_token(request: Request) -> str | None:
        return request.query_params.get("token") or _bearer_token(
            request.headers.get("authorization")
        )

    async def _reject_websocket(
        websocket: WebSocket,
        *,
        code: int,
        reason: str,
        error: str,
    ) -> None:
        try:
            await websocket.send_text(
                json.dumps({"type": "register_ack", "ok": False, "error": error})
            )
        except Exception:
            pass
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    @app.websocket("/tunnel/connect")
    async def tunnel_connect(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=_REGISTER_TIMEOUT_SECONDS
            )
            register = json.loads(raw)
            if not isinstance(register, dict) or register.get("type") != "register":
                raise ValueError("first frame must be register")
        except WebSocketDisconnect:
            return
        except Exception:
            await _reject_websocket(
                websocket,
                code=4400,
                reason="invalid registration",
                error="expected a valid register message",
            )
            return

        agent_value = register.get("agent")
        if not isinstance(agent_value, str) or not agent_value.strip():
            await _reject_websocket(
                websocket,
                code=4400,
                reason="invalid agent",
                error="agent must be a non-empty string",
            )
            return
        agent_name = agent_value.strip()
        presented = websocket.query_params.get("token") or _bearer_token(
            websocket.headers.get("authorization")
        )
        body_token = register.get("token")
        if presented is None and isinstance(body_token, str):
            presented = body_token
        if not await _authorize(presented, agent_name):
            logger.warning("Rejected connector authentication for agent %s", agent_name)
            await _reject_websocket(
                websocket,
                code=4403,
                reason="unauthorized",
                error="tunnel registration unauthorized",
            )
            return

        try:
            agent_name, descriptors = validate_registration(agent_name, register.get("servers"))
        except ValueError:
            await _reject_websocket(
                websocket,
                code=4400,
                reason="invalid servers",
                error="invalid tunnel service registration",
            )
            return

        conn = ConnectorConnection(
            connector_id=uuid.uuid4().hex,
            websocket=websocket,
            agent_name=agent_name,
            servers=descriptors,
        )
        try:
            reg.add_connection(conn)
        except ValueError:
            await _reject_websocket(
                websocket,
                code=4400,
                reason="registration conflict",
                error="invalid or conflicting tunnel service registration",
            )
            return

        try:
            await conn._send_json(
                {
                    "type": "register_ack",
                    "ok": True,
                    "connector_id": conn.connector_id,
                }
            )
            while True:
                raw = await websocket.receive_text()
                conn.dispatch(json.loads(raw))
        except WebSocketDisconnect:
            pass
        except (json.JSONDecodeError, ValueError):
            logger.warning("Closing connector %s after a malformed frame", conn.connector_id)
            try:
                await websocket.close(code=4400, reason="malformed tunnel frame")
            except Exception:
                pass
        except Exception as exc:
            logger.warning(
                "Connector %s WebSocket failed (%s)",
                conn.connector_id,
                type(exc).__name__,
            )
            try:
                await websocket.close(code=1011, reason="tunnel connection failed")
            except Exception:
                pass
        finally:
            conn.fail_all()
            reg.remove_connection(conn.connector_id, conn)

    def _proxy_authorized(request: Request) -> bool:
        presented = _bearer_token(request.headers.get("authorization"))
        return presented is not None and _secrets_equal(presented, reg.proxy_token)

    async def _proxy(agent: str, server: str, path: str, request: Request) -> Response:
        if not _proxy_authorized(request):
            return Response("tunnel proxy authentication required", status_code=401)
        conn = reg.find_connection(agent, server)
        if conn is None or not conn.active:
            return Response("tunnel server not connected", status_code=503)

        body = await request.body()
        payload = {
            "method": request.method,
            "path": f"/{path}" if path else "",
            "query": request.url.query,
            "headers": _filter_headers(dict(request.headers)),
            "body": body.decode("utf-8", errors="replace"),
        }
        try:
            queue = await conn.request(server, payload)
        except TunnelConnectionError:
            return Response("tunnel request could not be sent", status_code=502)

        try:
            head = await asyncio.wait_for(queue.get(), timeout=_HEAD_TIMEOUT_SECONDS)
        except TimeoutError:
            await conn.cancel(queue.request_id)
            return Response("timed out waiting for tunnel response", status_code=504)
        except asyncio.CancelledError:
            await asyncio.shield(conn.cancel(queue.request_id))
            raise

        if head[0] == "error":
            return Response("tunnel request failed", status_code=502)
        if head[0] != "head":
            await conn.cancel(queue.request_id)
            return Response("invalid tunnel response", status_code=502)

        async def generate():
            completed = False
            try:
                while True:
                    event = await queue.get()
                    if event[0] == "chunk":
                        yield event[1].encode("utf-8")
                    elif event[0] == "end":
                        completed = True
                        break
                    elif event[0] == "error":
                        raise TunnelStreamError("tunnel response interrupted")
                    else:
                        raise TunnelStreamError("invalid tunnel response sequence")
            finally:
                if not completed:
                    await conn.cancel(queue.request_id)

        return StreamingResponse(
            generate(),
            status_code=head[1],
            headers=_filter_headers(head[2]),
        )

    @app.api_route("/tunnel/mcp/{agent}/{server}", methods=["GET", "POST", "DELETE"])
    async def proxy_root(agent: str, server: str, request: Request) -> Response:
        return await _proxy(agent, server, "", request)

    @app.api_route(
        "/tunnel/mcp/{agent}/{server}/{path:path}",
        methods=["GET", "POST", "DELETE"],
    )
    async def proxy_path(agent: str, server: str, path: str, request: Request) -> Response:
        return await _proxy(agent, server, path, request)

    @app.get("/tunnel/servers")
    async def list_servers(request: Request, agent: str | None = None) -> Response:
        if agent is None or not agent.strip():
            return JSONResponse({"error": "agent is required"}, status_code=400)
        agent_name = agent.strip()
        presented = _request_tunnel_token(request)
        if not await _authorize(presented, agent_name):
            return JSONResponse({"error": "tunnel authentication failed"}, status_code=403)
        return JSONResponse(
            {
                "agent": agent_name,
                "servers": [descriptor.model_dump() for descriptor in reg.list_servers(agent_name)],
            }
        )

    logger.info(
        "Tunnel routes mounted (authentication=%s, allowed_agents=%s)",
        "custom" if auth is not None else "token",
        "any" if agents is None else len(agents),
    )


__all__ = [
    "ConnectorConnection",
    "TunnelConnectionError",
    "TunnelStreamError",
    "mount_tunnel",
]
