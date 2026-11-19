"""Enterprise-side outbound connector. Only configured local tools are reachable."""

from __future__ import annotations

import asyncio
import codecs
import logging
import math
import random
import ssl
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# The transport, response streams and MCP client must use the same backend,
# even when both httpx packages are installed in a legacy MCP environment.
from ksadk._mcp_compat import httpx_module

httpx = httpx_module()
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import Connect

from .protocol import (
    LOCAL_ERROR_CODES,
    VERSION,
    TunnelError,
    TunnelLimits,
    decode,
    encode,
    identifier,
)
from .services import LocalAPIService, LocalMCPService, prepare_request, response_headers

logger = logging.getLogger(__name__)


class _DirectConnect(Connect):
    def handle_redirect(self, uri: str) -> None:
        # A gateway redirect must never forward its Authorization credential.
        raise TunnelError("gateway_redirect_rejected")


class TunnelConnector:
    """A reconnecting connector. Disconnected or timed-out calls are never replayed."""

    def __init__(
        self,
        cloud_url: str,
        *,
        agent_id: str | None = None,
        token: str,
        services: list[LocalMCPService | LocalAPIService],
        gateway_headers: dict[str, str] | None = None,
        verify_tls: bool = True,
        limits: TunnelLimits | None = None,
        reconnect_min: float = 0.5,
        reconnect_max: float = 30.0,
        reconnect_handshake_grace: float = 120.0,
    ):
        parsed = urlsplit(cloud_url)
        if (
            parsed.scheme not in {"http", "https", "ws", "wss"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("cloud_url must be a base URL without credentials/query/fragment")
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        if not isinstance(verify_tls, bool):
            raise TypeError("verify_tls must be a bool")
        self._ssl_context = None
        if scheme == "wss":
            if verify_tls:
                self._ssl_context = ssl.create_default_context()
            else:
                self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self._ssl_context.check_hostname = False
                self._ssl_context.verify_mode = ssl.CERT_NONE
        self._url = urlunsplit(
            (scheme, parsed.netloc, parsed.path.rstrip("/") + "/tunnel/connect", "", "")
        )
        self.agent_id = identifier(agent_id, "agent_id") if agent_id is not None else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("a non-empty tunnel token is required")
        self._token = token
        self._headers = dict(gateway_headers or {})
        self.limits = limits or TunnelLimits()
        if (
            not isinstance(services, (list, tuple))
            or not services
            or len(services) > self.limits.max_services
            or any(
                not isinstance(service, (LocalMCPService, LocalAPIService)) for service in services
            )
            or len({s.name for s in services}) != len(services)
        ):
            raise ValueError("services must be non-empty, unique and within max_services")
        self._services = {s.name: s for s in services}
        self._descriptors = [service.descriptor() for service in services]
        if any(
            max(len(item.get("tools", [])), len(item.get("tool_filter") or ()))
            > self.limits.max_tools
            for item in self._descriptors
        ):
            raise ValueError("service tool count exceeds max_tools")
        if (
            not math.isfinite(reconnect_min)
            or not math.isfinite(reconnect_max)
            or not math.isfinite(reconnect_handshake_grace)
            or reconnect_min <= 0
            or reconnect_max < reconnect_min
            or reconnect_handshake_grace < 0
        ):
            raise ValueError("invalid reconnect bounds")
        self._reconnect_min, self._reconnect_max = reconnect_min, reconnect_max
        self._handshake_grace = reconnect_handshake_grace
        self._registrations = 0
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self._ws: Any = None
        self._running = False

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self, *, reconnect: bool = True) -> None:
        if self._running:
            raise RuntimeError("connector is already running")
        self._running = True
        delay = self._reconnect_min
        recovery_deadline = 0.0
        try:
            while not self._stop.is_set():
                registrations = self._registrations
                try:
                    try:
                        await self._run_connection()
                    finally:
                        if self._registrations != registrations:
                            # Begin a fixed recovery window only after a new
                            # successful registration. Rejected attempts never
                            # move its deadline, including duplicate ownership
                            # while an old WebSocket is still being cleaned up.
                            recovery_deadline = (
                                asyncio.get_running_loop().time() + self._handshake_grace
                            )
                            delay = self._reconnect_min
                    delay = self._reconnect_min
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd is not None else None
                    if code == 4403:
                        raise TunnelError("unauthorized") from None
                    if code == 4400:
                        raise TunnelError("protocol_rejected") from None
                    if code == 4409 and not (
                        reconnect and asyncio.get_running_loop().time() < recovery_deadline
                    ):
                        raise TunnelError("duplicate_service") from None
                    if not reconnect and not self._stop.is_set():
                        raise TunnelError("disconnected") from None
                except TunnelError as exc:
                    if not (
                        exc.code == "duplicate_service"
                        and reconnect
                        and asyncio.get_running_loop().time() < recovery_deadline
                    ):
                        raise
                except InvalidHandshake as exc:
                    # InvalidStatusCode is a deprecated dynamic export in newer
                    # websockets. Its stable base works across our 12–15 range.
                    status = getattr(exc, "status_code", None)
                    recovering = (
                        status == 403
                        and reconnect
                        and asyncio.get_running_loop().time() < recovery_deadline
                    )
                    if status not in {429, 500, 502, 503, 504} and not recovering:
                        raise TunnelError("gateway_handshake_rejected") from None
                    if not reconnect:
                        raise TunnelError("gateway_unavailable") from None
                except ssl.SSLCertVerificationError:
                    raise TunnelError("tls_verification_failed") from None
                except (OSError, TimeoutError):
                    if not reconnect:
                        raise TunnelError("connection_failed") from None
                if self._stop.is_set() or not reconnect:
                    break
                logger.info("Tool tunnel disconnected; reconnecting without replaying calls")
                try:
                    await asyncio.wait_for(self._stop.wait(), delay * random.uniform(0.8, 1.2))
                except TimeoutError:
                    pass
                delay = min(delay * 2, self._reconnect_max)
        finally:
            self._running = False
            self.connected.clear()

    async def _run_connection(self) -> None:
        tasks: dict[str, asyncio.Task] = {}
        send_lock = asyncio.Lock()
        async with _DirectConnect(
            self._url,
            extra_headers=self._headers,
            max_size=self.limits.max_frame_bytes,
            max_queue=self.limits.max_inflight,
            open_timeout=self.limits.registration_timeout,
            close_timeout=self.limits.send_timeout,
            ping_interval=self.limits.heartbeat_interval,
            ping_timeout=self.limits.idle_timeout,
            **({"ssl": self._ssl_context} if self._ssl_context is not None else {}),
        ) as ws:
            self._ws = ws

            async def send(frame):
                raw = encode(frame, self.limits)
                async with asyncio.timeout(self.limits.send_timeout):
                    async with send_lock:
                        await ws.send(raw)

            async def heartbeat():
                while True:
                    await asyncio.sleep(self.limits.heartbeat_interval)
                    await send({"type": "heartbeat"})

            heartbeat_task = None
            try:
                await send(
                    {
                        "type": "register",
                        "version": VERSION,
                        **({"agent_id": self.agent_id} if self.agent_id is not None else {}),
                        "token": self._token,
                        "services": self._descriptors,
                    }
                )
                async with asyncio.timeout(self.limits.registration_timeout):
                    try:
                        ack = decode(await ws.recv(), self.limits)
                    except (ValueError, TypeError):
                        raise TunnelError("invalid_registration_ack") from None
                if (
                    set(ack) == {"type", "version", "error"}
                    and ack["type"] == "registration_rejected"
                    and ack["version"] == VERSION
                    and ack["error"] in ("unauthorized", "duplicate_service")
                ):
                    raise TunnelError(ack["error"])
                if (
                    set(ack) != {"type", "version", "connector_id"}
                    or ack.get("type") != "registered"
                    or ack.get("version") != VERSION
                ):
                    raise TunnelError("invalid_registration_ack")
                try:
                    identifier(ack["connector_id"], "connector id")
                except ValueError:
                    raise TunnelError("invalid_registration_ack") from None
                self.connected.set()
                self._registrations += 1
                heartbeat_task = asyncio.create_task(heartbeat())

                def done(request_id, task):
                    tasks.pop(request_id, None)
                    if not task.cancelled():
                        task.exception()  # receive loop observes disconnect; never leak task errors

                while not self._stop.is_set():
                    async with asyncio.timeout(self.limits.idle_timeout):
                        try:
                            frame = decode(await ws.recv(), self.limits)
                        except (ValueError, TypeError):
                            raise TunnelError("invalid_request") from None
                    if frame == {"type": "heartbeat_ack"}:
                        continue
                    request_id = frame.get("id")
                    try:
                        identifier(request_id, "request id")
                    except ValueError:
                        raise TunnelError("invalid_request") from None
                    if frame.get("type") == "http_cancel" and set(frame) == {"type", "id"}:
                        if request_id in tasks:
                            tasks[request_id].cancel()
                        continue
                    if (
                        set(frame)
                        != {"type", "id", "server", "method", "path", "query", "headers", "body"}
                        or frame.get("type") != "http_request"
                        or request_id in tasks
                    ):
                        raise TunnelError("invalid_request")
                    # One admitted MCP operation may hold a GET event stream,
                    # notification POST and tool POST concurrently. The cloud
                    # bounds logical operations; this bounds their HTTP tasks.
                    if len(tasks) >= 3 * self.limits.max_inflight:
                        await send({"type": "http_error", "id": request_id, "error": "busy"})
                        continue
                    task = asyncio.create_task(self._handle(frame, send))
                    tasks[request_id] = task
                    task.add_done_callback(lambda t, rid=request_id: done(rid, t))
            finally:
                self.connected.clear()
                self._ws = None
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                active = [*tasks.values(), *([heartbeat_task] if heartbeat_task else [])]
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)

    async def _handle(self, frame: dict, send) -> None:
        error = "local_service_error"
        service = None
        try:
            async with asyncio.timeout(self.limits.request_timeout):
                service = (
                    self._services.get(frame["server"])
                    if isinstance(frame["server"], str)
                    else None
                )
                if service is None:
                    raise TunnelError("forbidden")
                request = prepare_request(service, frame)
                # A client per HTTP request keeps cookies and authentication
                # state isolated between local services and cloud sessions.
                async with httpx.AsyncClient(
                    timeout=self.limits.request_timeout,
                    trust_env=False,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                ) as client:
                    async with client.stream(
                        request.method,
                        request.url,
                        headers=request.headers,
                        content=request.content,
                    ) as response:
                        await send(
                            {
                                "type": "http_response",
                                "id": frame["id"],
                                "status": response.status_code,
                                "headers": response_headers(response.headers),
                            }
                        )
                        decoder = codecs.getincrementaldecoder("utf-8")()
                        received = 0

                        async def emit(text):
                            # JSON escaping can cost six bytes per character.
                            # Split only an already received network chunk;
                            # never ask httpx to buffer an SSE event to a size.
                            step = max(1, (self.limits.max_frame_bytes - 256) // 6)
                            for offset in range(0, len(text), step):
                                await send(
                                    {
                                        "type": "http_chunk",
                                        "id": frame["id"],
                                        "data": text[offset : offset + step],
                                    }
                                )

                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > self.limits.max_response_bytes:
                                raise TunnelError("frame_too_large")
                            await emit(decoder.decode(chunk, final=False))
                        await emit(decoder.decode(b"", final=True))
                await send({"type": "http_end", "id": frame["id"]})
                return
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = "timeout"
        except TunnelError as exc:
            error = exc.code if exc.code in LOCAL_ERROR_CODES else "local_service_error"
        except Exception:
            # HTTP and decoding errors can contain local URLs, headers and
            # payloads. Only a fixed protocol error code may reach the cloud.
            if isinstance(service, LocalMCPService):
                error = "local_mcp_error"
        await send({"type": "http_error", "id": frame["id"], "error": error})
