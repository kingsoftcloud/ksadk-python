"""Runtime-scoped peer discovery and authenticated streaming HTTP forwarding.

Only the dedicated internal listener uses this protocol. Connector credentials
never cross it. Every channel is mutually authenticated and every subsequent
frame is direction-bound, sequenced and HMAC authenticated.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import socket
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# The transport, response streams and MCP client must use the same backend,
# even when both httpx packages are installed in a legacy MCP environment.
from ksadk._mcp_compat import httpx_module

httpx = httpx_module()
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import Connect
from websockets.legacy.server import serve

from .protocol import (
    IncompletePeerDiscovery,
    TunnelError,
    TunnelLimits,
    decode_request_body,
    encode,
    identifier,
)

_PATH = "/ksadk-tunnel-peer"
_AUTH_WINDOW = 30
_CHALLENGE_HEADER = "X-KsADK-Peer-Challenge"
_MAX_LEGACY_NONCES = 4096
logger = logging.getLogger(__name__)
_ERROR_CODES = frozenset(
    {
        "busy",
        "forbidden",
        "disconnected",
        "timeout",
        "frame_too_large",
        "local_service_error",
        "local_mcp_error",
        "invalid_request",
    }
)
_current_operation = ContextVar("ksadk_tunnel_peer_operation", default=None)


@dataclass
class PeerOperation:
    deadline: asyncio.Timeout
    error: str | None = None

    def fail(self, code):
        self.error = code
        if not self.deadline.expired():
            self.deadline.reschedule(asyncio.get_running_loop().time())


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    ).encode()


def _mac(key, *parts):
    return hmac.new(
        key,
        b"\n".join(part if isinstance(part, bytes) else str(part).encode() for part in parts),
        hashlib.sha256,
    ).hexdigest()


def _wire_error(frame):
    code = frame.get("error")
    return code if isinstance(code, str) and code in _ERROR_CODES else "local_service_error"


def validate_owner(value):
    if not isinstance(value, dict) or set(value) != {"address", "instance_id"}:
        raise TunnelError("invalid_peer_owner")
    identifier(value["instance_id"], "peer instance")
    parsed = urlsplit("ws://" + value["address"])
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise TunnelError("invalid_peer_owner")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise TunnelError("invalid_peer_owner") from None
    return value


@dataclass(frozen=True)
class PeerConfig:
    runtime_id: str
    service: str
    secret: str = field(repr=False)
    port: int = 8092
    host: str = "0.0.0.0"
    advertised_host: str | None = None
    discovery_timeout: float = 3.0
    max_peers: int = 64
    ownership_cache_ttl: float = 2.0
    topology_cache_ttl: float = 1.0

    def __post_init__(self):
        if (
            not self.runtime_id
            or not self.service
            or not isinstance(self.secret, str)
            or len(self.secret.encode()) < 32
            or not 0 <= self.port <= 65535
            or self.discovery_timeout <= 0
            or self.max_peers < 1
            or any(
                not math.isfinite(value) or value <= 0
                for value in (
                    self.discovery_timeout,
                    self.ownership_cache_ttl,
                    self.topology_cache_ttl,
                )
            )
        ):
            raise ValueError("invalid runtime peer configuration (secret requires 32 bytes)")

    @classmethod
    def from_env(cls, *, tunnel_token: str | None = None):
        secret = os.getenv("KSADK_TUNNEL_PEER_SECRET", "")
        runtime_id = os.getenv("AGENT_RUNTIME_ID", "").strip()
        if not runtime_id:
            if secret:
                raise ValueError("AGENT_RUNTIME_ID is required for runtime peer discovery")
            return None  # Local runs have no platform runtime identity.
        if not secret:
            if not tunnel_token or not tunnel_token.strip():
                return None
            secret = hmac.new(
                tunnel_token.encode(),
                ("ksadk-tunnel-peer-v1:" + runtime_id).encode(),
                hashlib.sha256,
            ).hexdigest()
        return cls(
            runtime_id,
            os.getenv("KSADK_TUNNEL_PEER_SERVICE") or runtime_id + "-headless",
            secret,
            port=int(os.getenv("KSADK_TUNNEL_PEER_PORT", "8092")),
        )


@dataclass
class PeerCatalog:
    connections: list[tuple[str, RemoteConnector]]
    unavailable: bool
    expires_at: float


class _DirectConnect(Connect):
    def handle_redirect(self, uri):
        raise TunnelError("peer_auth_failed")


class SignedSocket:
    def __init__(self, ws, key, limits, *, server=False, on_close=None):
        self.ws, self.key, self.limits = ws, key, limits
        self.on_close = on_close
        self.send_direction, self.recv_direction = ("s2c", "c2s") if server else ("c2s", "s2c")
        self.sent = self.received = 0

    async def send(self, payload):
        encode(payload, self.limits)
        frame = {
            "seq": self.sent,
            "payload": payload,
            "mac": _mac(self.key, self.send_direction, self.sent, _json(payload)),
        }
        async with asyncio.timeout(self.limits.send_timeout):
            await self.ws.send(
                json.dumps(frame, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
            )
        self.sent += 1

    async def receive(self):
        async with asyncio.timeout(self.limits.request_timeout):
            raw = await self.ws.recv()
        if not isinstance(raw, str) or len(raw.encode()) > self.limits.max_frame_bytes + 1024:
            raise TunnelError("peer_auth_failed")
        try:
            frame = json.loads(raw)
            if (
                set(frame) != {"seq", "payload", "mac"}
                or type(frame["seq"]) is not int
                or frame["seq"] != self.received
                or not isinstance(frame["mac"], str)
                or not hmac.compare_digest(
                    frame["mac"],
                    _mac(self.key, self.recv_direction, frame["seq"], _json(frame["payload"])),
                )
            ):
                raise ValueError
            encode(frame["payload"], self.limits)
        except (ValueError, TypeError, KeyError):
            raise TunnelError("peer_auth_failed") from None
        self.received += 1
        return frame["payload"]

    async def close(self):
        try:
            async with asyncio.timeout(self.limits.send_timeout):
                await self.ws.close()
        except TimeoutError:
            self.ws.fail_connection()
        finally:
            if self.on_close is not None:
                self.on_close(self)


@dataclass(eq=False)
class RemoteConnector:
    network: object = field(repr=False)
    agent_id: str
    connector_id: str
    owner: dict
    services: dict
    limits: TunnelLimits
    operations: set = field(default_factory=set, repr=False)
    closed: bool = False

    async def forward(self, service, request):
        return await self.network.forward(self, service, request)


class PeerStream(httpx.AsyncByteStream):
    def __init__(self, channel, on_failure):
        self.channel, self.done = channel, False
        self.operation = _current_operation.get()
        self.on_failure = on_failure

    async def __aiter__(self):
        received = 0
        try:
            while True:
                frame = await self.channel.receive()
                if frame.get("type") == "http_end":
                    self.done = True
                    return
                if frame.get("type") == "http_error":
                    raise TunnelError(_wire_error(frame))
                if frame.get("type") != "http_chunk" or not isinstance(frame.get("data"), str):
                    raise TunnelError("invalid_result")
                data = frame["data"].encode()
                received += len(data)
                if received > self.channel.limits.max_response_bytes:
                    raise TunnelError("frame_too_large")
                yield data
        except (ConnectionClosed, OSError):
            self.on_failure()
            if self.operation is not None:
                self.operation.fail("disconnected")
            raise TunnelError("disconnected") from None
        except Exception as exc:
            self.on_failure()
            # MCP can be waiting on a parsed SSE message. Wake exactly the
            # parent operation; another call may share this owner/generation.
            if self.operation is not None:
                self.operation.fail(
                    exc.code if isinstance(exc, TunnelError) else "local_service_error"
                )
            raise
        finally:
            await self.aclose()

    async def aclose(self):
        channel, self.channel = self.channel, None
        if channel is None:
            return
        try:
            if not self.done:
                await channel.send({"type": "http_cancel"})
        except Exception:
            pass
        finally:
            await channel.close()


class PeerNetwork:
    def __init__(self, config, registry_provider, *, limits=None, resolver=None):
        self.config, self.registry_provider = config, registry_provider
        self.limits = limits or TunnelLimits()
        self.instance_id = uuid.uuid4().hex
        self.key = hmac.new(
            config.secret.encode(), ("ksadk-peer-v1\n" + config.runtime_id).encode(), hashlib.sha256
        ).digest()
        self.server = None
        self.owner = None
        self._resolver = resolver
        self._legacy_nonces = {}
        self._remotes = {}
        self._catalogs = {}
        self._discovery_lock = asyncio.Lock()
        self._topology = set()
        self._topology_expires_at = 0.0
        self._handlers = set()
        self._channels = {}
        self._start_task = None
        self._retry_start_at = 0.0
        self._closing = False

    @contextmanager
    def operation_scope(self, deadline):
        operation = PeerOperation(deadline)
        token = _current_operation.set(operation)
        try:
            yield operation
        finally:
            _current_operation.reset(token)

    def request_start(self):
        """Schedule one shared attempt without blocking Connector registration or I/O."""
        if self._closing:
            raise TunnelError("closed")
        if self.server is not None or time.monotonic() < self._retry_start_at:
            return None
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self._try_start_listener())
        return self._start_task

    async def start(self) -> bool:
        attempt = self.request_start()
        if attempt is not None:
            await asyncio.shield(attempt)
        return self.server is not None

    async def _try_start_listener(self):
        try:
            async with asyncio.timeout(self.config.discovery_timeout):
                await self._start_listener()
        except (OSError, ValueError) as exc:
            self._retry_start_at = time.monotonic() + self.config.topology_cache_ttl
            logger.warning(
                "Tunnel peer listener unavailable (%s); using local connectors", type(exc).__name__
            )

    async def _start_listener(self):
        host = self.config.advertised_host
        if host is None:
            fqdn = await asyncio.to_thread(socket.getfqdn)
            records = await asyncio.get_running_loop().getaddrinfo(
                fqdn, None, type=socket.SOCK_STREAM
            )
            if not records:
                raise OSError("no peer listener address")
            host = records[0][4][0]
        ipaddress.ip_address(host)
        self.server = await serve(
            self._accept,
            self.config.host,
            self.config.port,
            extra_headers=lambda path, headers: {_CHALLENGE_HEADER: uuid.uuid4().hex},
            max_size=self.limits.max_frame_bytes + 1024,
            max_queue=8,
            open_timeout=self.limits.registration_timeout,
            close_timeout=self.limits.send_timeout,
            ping_interval=self.limits.heartbeat_interval,
            ping_timeout=self.limits.idle_timeout,
        )
        port = self.server.sockets[0].getsockname()[1]
        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.owner = {"address": address, "instance_id": self.instance_id}

    async def addresses(self):
        if time.monotonic() < self._topology_expires_at:
            return set(self._topology)
        async with asyncio.timeout(self.config.discovery_timeout):
            if self._resolver is not None:
                values = await self._resolver()
            else:
                records = await asyncio.get_running_loop().getaddrinfo(
                    self.config.service, self.config.port, type=socket.SOCK_STREAM
                )
                values = [
                    (
                        f"[{r[4][0]}]:{self.config.port}"
                        if ":" in r[4][0]
                        else f"{r[4][0]}:{self.config.port}"
                    )
                    for r in records
                ]
        addresses = set(values)
        if len(addresses) > self.config.max_peers:
            raise TunnelError("peer_topology_limit")
        for address in addresses:
            validate_owner({"address": address, "instance_id": self.instance_id})
        self._topology = addresses
        self._topology_expires_at = time.monotonic() + self.config.topology_cache_ttl
        return set(addresses)

    async def _open(self, address, expected_instance=None):
        if self._closing:
            raise TunnelError("disconnected")
        ws = None
        try:
            ws = await _DirectConnect(
                f"ws://{address}{_PATH}",
                open_timeout=self.config.discovery_timeout,
                close_timeout=self.limits.send_timeout,
                max_size=self.limits.max_frame_bytes + 1024,
                max_queue=8,
            )
            hello = {
                "type": "hello",
                "runtime": self.config.runtime_id,
                "timestamp": int(time.time()),
                "nonce": uuid.uuid4().hex,
            }
            challenge = ws.response_headers.get(_CHALLENGE_HEADER)
            if challenge is not None:
                hello["challenge"] = challenge
            hello["signature"] = _mac(self.key, _json(hello))
            await ws.send(json.dumps(hello))
            async with asyncio.timeout(self.config.discovery_timeout):
                ack = json.loads(await ws.recv())
            signature = ack.pop("signature", None)
            if (
                set(ack) != {"type", "nonce", "instance_id"}
                or ack["type"] != "hello_ack"
                or not isinstance(signature, str)
                or not hmac.compare_digest(signature, _mac(self.key, hello["nonce"], _json(ack)))
                or (expected_instance is not None and ack["instance_id"] != expected_instance)
            ):
                raise TunnelError("peer_auth_failed")
            identifier(ack["nonce"], "peer nonce")
            identifier(ack["instance_id"], "peer instance")
            if self._closing:
                raise TunnelError("disconnected")
            session_key = bytes.fromhex(_mac(self.key, "session", hello["nonce"], ack["nonce"]))
            channel = SignedSocket(
                ws,
                session_key,
                self.limits,
                on_close=lambda socket: self._channels.pop(socket, None),
            )
            self._channels[channel] = _current_operation.get()
            return channel, ack["instance_id"]
        except BaseException:
            if ws is not None:
                await ws.close()
            raise

    async def _authenticate(self, ws):
        async with asyncio.timeout(self.config.discovery_timeout):
            hello = json.loads(await ws.recv())
        signature = hello.pop("signature", None)
        now = time.time()
        fields = {"type", "runtime", "timestamp", "nonce"}
        if "challenge" in hello:
            fields.add("challenge")
            if hello["challenge"] != ws.response_headers[_CHALLENGE_HEADER]:
                raise TunnelError("peer_auth_failed")
        if (
            set(hello) != fields
            or hello["type"] != "hello"
            or hello["runtime"] != self.config.runtime_id
            or type(hello["timestamp"]) is not int
            or abs(now - hello["timestamp"]) > _AUTH_WINDOW
            or not isinstance(hello["nonce"], str)
            or len(hello["nonce"]) != 32
            or not isinstance(signature, str)
            or not hmac.compare_digest(signature, _mac(self.key, _json(hello)))
        ):
            raise TunnelError("peer_auth_failed")
        if "challenge" not in hello:
            # Rolling upgrades: old clients cannot bind their signature to this socket.
            self._legacy_nonces = {
                n: seen for n, seen in self._legacy_nonces.items() if now - seen <= 2 * _AUTH_WINDOW
            }
            if (
                hello["nonce"] in self._legacy_nonces
                or len(self._legacy_nonces) >= _MAX_LEGACY_NONCES
            ):
                raise TunnelError("peer_auth_failed")
            self._legacy_nonces[hello["nonce"]] = now
        ack = {"type": "hello_ack", "nonce": uuid.uuid4().hex, "instance_id": self.instance_id}
        ack["signature"] = _mac(self.key, hello["nonce"], _json(ack))
        await ws.send(json.dumps(ack))
        session_key = bytes.fromhex(_mac(self.key, "session", hello["nonce"], ack["nonce"]))
        return SignedSocket(ws, session_key, self.limits, server=True)

    async def _accept(self, ws, path):
        task = asyncio.current_task()
        self._handlers.add(task)
        relay = watcher = None
        try:
            if path != _PATH or len(self._handlers) > 3 * self.limits.max_inflight:
                return
            channel = await self._authenticate(ws)
            payload = await channel.receive()
            registry = self.registry_provider()
            agent = identifier(payload.get("agent_id"), "agent_id")
            if payload.get("type") == "discover" and set(payload) == {"type", "agent_id"}:
                services = [
                    {
                        "name": service,
                        "connector_id": conn.connector_id,
                        "descriptor": conn.services[service],
                    }
                    for service, conn in (
                        registry.connections(agent) if registry is not None else []
                    )
                ]
                await channel.send({"type": "discovery", "services": services})
                return
            if registry is None:
                raise TunnelError("disconnected")
            if set(payload) != {
                "type",
                "agent_id",
                "service",
                "connector_id",
                "method",
                "path",
                "query",
                "headers",
                "body",
            }:
                raise TunnelError("invalid_request")
            service = identifier(payload["service"], "service")
            generation = identifier(payload["connector_id"], "connector_id")
            conn = registry._connections.get((agent, service))
            if (
                payload["type"] != "http_request"
                or conn is None
                or conn.closed
                or generation != conn.connector_id
            ):
                raise TunnelError("disconnected")
            if (
                payload["method"] not in {"GET", "POST", "DELETE"}
                or payload["query"] != ""
                or not isinstance(payload["path"], str)
                or "/" in payload["path"]
                or not isinstance(payload["body"], str)
                or not isinstance(payload["headers"], dict)
            ):
                raise TunnelError("invalid_request")
            request = httpx.Request(
                payload["method"],
                f"http://tunnel/{payload['path']}",
                headers=payload["headers"],
                content=payload["body"].encode(),
            )
            relay = asyncio.create_task(self._relay(channel, conn, service, request))
            watcher = asyncio.create_task(channel.receive())
            done, _ = await asyncio.wait({relay, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                frame = watcher.result()
                if frame != {"type": "http_cancel"}:
                    raise TunnelError("invalid_request")
            else:
                await relay
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                if "channel" in locals():
                    safe = {
                        "busy",
                        "forbidden",
                        "disconnected",
                        "timeout",
                        "frame_too_large",
                        "local_service_error",
                        "local_mcp_error",
                        "invalid_request",
                    }
                    code = (
                        exc.code
                        if isinstance(exc, TunnelError) and exc.code in safe
                        else (
                            "timeout"
                            if isinstance(exc, TimeoutError)
                            else (
                                "disconnected"
                                if isinstance(exc, (ConnectionClosed, OSError))
                                else "local_service_error"
                            )
                        )
                    )
                    await channel.send({"type": "http_error", "error": code})
            except Exception:
                pass
        finally:
            for active in (relay, watcher):
                if active is not None:
                    active.cancel()
            await asyncio.gather(
                *(t for t in (relay, watcher) if t is not None), return_exceptions=True
            )
            self._handlers.discard(task)
            try:
                async with asyncio.timeout(self.limits.send_timeout):
                    await ws.close()
            except TimeoutError:
                ws.fail_connection()

    async def _relay(self, channel, connection, service, request):
        response = await connection.forward(service, request)
        try:
            await channel.send(
                {
                    "type": "http_response",
                    "status": response.status_code,
                    "headers": dict(response.headers),
                }
            )
            async for data in response.aiter_bytes():
                await channel.send({"type": "http_chunk", "data": data.decode("utf-8")})
            await channel.send({"type": "http_end"})
        finally:
            await response.aclose()

    async def _discover_at(self, address, agent_id, expected_instance=None):
        channel, instance = await self._open(address, expected_instance)
        try:
            await channel.send({"type": "discover", "agent_id": agent_id})
            result = await channel.receive()
            if result.get("type") != "discovery" or not isinstance(result.get("services"), list):
                raise TunnelError("invalid_result")
            return {"address": address, "instance_id": instance}, self._parse_services(
                result["services"]
            )
        finally:
            await channel.close()

    def _parse_services(self, services):
        from .server import _validate_descriptor

        # A peer frame is size-bounded. The service count limit is per Connector.
        descriptors = {}
        for item in services:
            if not isinstance(item, dict) or set(item) != {"name", "connector_id", "descriptor"}:
                raise TunnelError("invalid_result")
            descriptor = item["descriptor"]
            if not isinstance(descriptor, dict) or descriptor.get("name") != item["name"]:
                raise TunnelError("invalid_result")
            try:
                name = identifier(item["name"], "service")
                generation = identifier(item["connector_id"], "connector_id")
                _validate_descriptor(descriptor, self.limits)
            except (KeyError, ValueError):
                raise TunnelError("invalid_result") from None
            group = descriptors.setdefault(generation, {})
            if name in group or len(group) >= self.limits.max_services:
                raise TunnelError("invalid_result")
            group[name] = descriptor
        return descriptors

    def _remember(self, owner, agent_id, descriptors):
        result = []
        for generation, group in descriptors.items():
            key = (owner["address"], owner["instance_id"], agent_id, generation)
            connection = self._remotes.get(key)
            if connection is None:
                connection = RemoteConnector(self, agent_id, generation, owner, {}, self.limits)
                self._remotes[key] = connection
            connection.services = group
            result.extend((name, connection) for name in group)
        return result

    def invalidate(self, owner):
        """Expire this owner's cached routes without cancelling other in-flight calls."""
        # A subsequent authenticated lookup may confirm this exact generation.
        # Its outstanding operation deadlines still consume the same budget.
        for catalog in self._catalogs.values():
            if any(connection.owner == owner for _, connection in catalog.connections):
                catalog.expires_at = 0.0
        self._topology_expires_at = 0.0

    async def _probe(self, address, agent_id):
        # Bound the whole discovery exchange, not only the TCP handshake.
        async with asyncio.timeout(self.config.discovery_timeout):
            return await self._discover_at(address, agent_id)

    async def _refresh_catalog(self, agent_id):
        try:
            addresses = await self.addresses()
        except (OSError, TimeoutError):
            addresses = set()
            unavailable = True
        else:
            addresses.discard(self.owner["address"])
            unavailable = False
        results = await asyncio.gather(
            *(self._probe(addr, agent_id) for addr in addresses), return_exceptions=True
        )
        confirmed = []
        for result in results:
            if isinstance(result, (OSError, TimeoutError, ConnectionClosed, InvalidHandshake)):
                # DNS may still advertise a terminating or not-yet-listening pod.
                unavailable = True
                continue
            if isinstance(result, BaseException):
                # Invalid authenticated metadata must never become a cached route.
                raise TunnelError("peer_discovery_unavailable") from None
            confirmed.append(result)
        # Publish ownership only after every responding peer passed validation.
        connections = [
            route
            for owner, descriptors in confirmed
            for route in self._remember(owner, agent_id, descriptors)
        ]
        if unavailable:
            logger.warning(
                "Tunnel peer discovery incomplete (%d confirmed services)", len(connections)
            )
        live = {
            (conn.owner["address"], conn.owner["instance_id"], agent_id, conn.connector_id)
            for _, conn in connections
        }
        self._remotes = {
            key: conn
            for key, conn in self._remotes.items()
            if key[2] != agent_id or key in live or conn.operations
        }
        # Retry missing peers sooner, including an empty/negative catalog.
        ttl = (
            min(self.config.ownership_cache_ttl, 0.25)
            if unavailable or not connections
            else self.config.ownership_cache_ttl
        )
        return PeerCatalog(connections, unavailable, time.monotonic() + ttl)

    async def discover(self, agent_id, *, has_local=False):
        async with self._discovery_lock:
            catalog = self._catalogs.get(agent_id)
            if catalog is None or time.monotonic() >= catalog.expires_at:
                catalog = await self._refresh_catalog(agent_id)
                self._catalogs[agent_id] = catalog
        if not catalog.connections and catalog.unavailable and not has_local:
            raise IncompletePeerDiscovery()
        return list(catalog.connections)

    async def resolve(self, agent_id, service, connector_id, owner):
        owner = validate_owner(owner)
        key = (owner["address"], owner["instance_id"], agent_id, connector_id)
        cached = self._remotes.get(key)
        catalog = self._catalogs.get(agent_id)
        if (
            cached is not None
            and service in cached.services
            and catalog is not None
            and time.monotonic() < catalog.expires_at
            and any(conn is cached for _, conn in catalog.connections)
        ):
            return cached
        try:
            if owner["address"] not in await self.addresses():
                raise TunnelError("disconnected")
            async with asyncio.timeout(self.config.discovery_timeout):
                found_owner, descriptors = await self._discover_at(
                    owner["address"], agent_id, expected_instance=owner["instance_id"]
                )
            for name, conn in self._remember(found_owner, agent_id, descriptors):
                if name == service and conn.connector_id == connector_id:
                    return conn
            raise TunnelError("disconnected")
        except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake):
            self.invalidate(owner)
            # Ownership lookup happens before the tool request is sent.
            raise TunnelError("disconnected") from None
        except TunnelError:
            self.invalidate(owner)
            raise

    async def forward(self, connection, service, request):
        body = decode_request_body(await request.aread())
        channel = None
        try:
            channel, _ = await self._open(
                connection.owner["address"], connection.owner["instance_id"]
            )
            await channel.send(
                {
                    "type": "http_request",
                    "agent_id": connection.agent_id,
                    "service": service,
                    "connector_id": connection.connector_id,
                    "method": request.method,
                    "path": request.url.path.strip("/"),
                    "query": request.url.query.decode("ascii"),
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            frame = await channel.receive()
            if frame.get("type") == "http_error":
                raise TunnelError(_wire_error(frame))
            if (
                frame.get("type") != "http_response"
                or type(frame.get("status")) is not int
                or not 100 <= frame["status"] <= 599
                or not isinstance(frame.get("headers"), dict)
            ):
                raise TunnelError("disconnected")
            return httpx.Response(
                frame["status"],
                headers=frame["headers"],
                stream=PeerStream(channel, lambda: self.invalidate(connection.owner)),
            )
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.invalidate(connection.owner)
            if channel is not None:
                try:
                    await channel.send({"type": "http_cancel"})
                except Exception:
                    pass
                await channel.close()
            operation = _current_operation.get()
            if operation is not None and isinstance(exc, Exception):
                operation.fail(exc.code if isinstance(exc, TunnelError) else "disconnected")
            raise

    async def close(self):
        self._closing = True
        if self._start_task is not None:
            self._start_task.cancel()
            await asyncio.gather(self._start_task, return_exceptions=True)
        # A disappearing caller pod must release outgoing streams as well as
        # its inbound listener. Wake each affected MCP operation independently.
        channels = list(self._channels.items())
        for _, operation in channels:
            if operation is not None:
                operation.fail("disconnected")
        await asyncio.gather(*(channel.close() for channel, _ in channels), return_exceptions=True)
        self._catalogs.clear()
        self._remotes.clear()
        self._topology.clear()
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2 * self.limits.send_timeout + 1)
            except TimeoutError:
                for task in list(self._handlers):
                    task.cancel()
                await asyncio.wait_for(
                    asyncio.gather(*self._handlers, return_exceptions=True),
                    self.limits.send_timeout,
                )
            self.server = None
