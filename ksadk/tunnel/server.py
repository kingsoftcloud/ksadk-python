"""Authenticated WebSocket ingress; deliberately exposes no HTTP proxy routes."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Mapping

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .protocol import VERSION, TunnelError, TunnelLimits, decode, encode, identifier
from .registry import ConnectorConnection, TunnelRegistry


@dataclass(frozen=True)
class TunnelAgentPolicy:
    """A stable deployment identity, secret and explicitly admitted service aliases."""

    agent_id: str
    token: str = field(repr=False)
    services: tuple[str, ...] | None = ()

    def __post_init__(self):
        identifier(self.agent_id, "agent_id")
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("a non-empty tunnel token is required")
        if self.services is None:
            return  # This deployment accepts service names declared by the authenticated connector.
        if not self.services or len(set(self.services)) != len(self.services):
            raise ValueError("services must be a non-empty unique alias allowlist")
        for service in self.services:
            identifier(service, "service")


def mount_tunnel(
    app: FastAPI,
    *,
    policies: list[TunnelAgentPolicy],
    registry: TunnelRegistry | None = None,
    limits: TunnelLimits | None = None,
) -> TunnelRegistry:
    """Mount /tunnel/connect. Caller closes the returned registry at app shutdown.

    Gateway Authorization belongs to the WS handshake. Application tunnel
    credentials are accepted only in the first register frame, never in a URL.
    """
    if hasattr(app.state, "mcp_tunnel"):
        raise ValueError("MCP tunnel already mounted")
    by_agent: Mapping[str, TunnelAgentPolicy] = {policy.agent_id: policy for policy in policies}
    if not by_agent or len(by_agent) != len(policies):
        raise ValueError("at least one unique tunnel agent policy is required")
    reg = registry if registry is not None else TunnelRegistry()
    if getattr(reg, "_owner_app", None) is not None:
        raise ValueError("MCP tunnel registry already belongs to another app")
    reg._owner_app = app
    bounds = limits or TunnelLimits()
    app.state.mcp_tunnel = reg

    app.websocket("/tunnel/connect")(_connector_endpoint(by_agent, reg, bounds))
    return reg


def _validate_descriptor(descriptor, bounds):
    if descriptor["protocol"] == "api":
        from mcp.types import Tool

        if set(descriptor) != {"name", "protocol", "tools"} or not isinstance(
            descriptor["tools"], list
        ):
            raise ValueError("invalid API descriptor")
        tools = [Tool.model_validate(tool) for tool in descriptor["tools"]]
        if len(tools) > bounds.max_tools or len({t.name for t in tools}) != len(tools):
            raise ValueError("invalid API tools")
    elif descriptor["protocol"] != "mcp":
        raise ValueError("invalid service protocol")
    elif set(descriptor) - {"name", "protocol", "tool_filter"}:
        raise ValueError("invalid MCP descriptor")
    filters = descriptor.get("tool_filter")
    if filters is not None and (
        not isinstance(filters, list)
        or len(filters) > bounds.max_tools
        or any(not isinstance(item, str) or not item for item in filters)
    ):
        raise ValueError("invalid MCP tool filter")


def _connector_endpoint(by_agent, reg, bounds):
    async def reject(ws, error, reason):
        # Gateways may drop WS close codes. Send a credential-free rejection
        # before closing so clients can stop retrying permanent registration errors.
        async with asyncio.timeout(bounds.send_timeout):
            await ws.send_text(
                encode(
                    {"type": "registration_rejected", "version": VERSION, "error": error}, bounds
                )
            )
            await ws.close(code=4409 if error == "duplicate_service" else 4403, reason=reason)

    async def connect(ws: WebSocket):
        await ws.accept()
        conn = None
        try:
            async with asyncio.timeout(bounds.registration_timeout):
                frame = decode(await ws.receive_text(), bounds)
            if (
                set(frame) - {"agent_id"} != {"type", "version", "token", "services"}
                or frame.get("type") != "register"
                or frame.get("version") != VERSION
            ):
                raise ValueError("invalid registration")
            if "agent_id" in frame:
                agent = identifier(frame["agent_id"], "agent_id")
            elif len(by_agent) == 1:
                agent = next(iter(by_agent))
            else:
                raise ValueError("agent_id required for a multi-agent host")
            policy = by_agent.get(agent)
            token = frame["token"]
            if (
                policy is None
                or not isinstance(token, str)
                or not secrets.compare_digest(token.encode(), policy.token.encode())
            ):
                await reject(ws, "unauthorized", "unauthorized")
                return
            services = frame["services"]
            if (
                not isinstance(services, list)
                or not services
                or len(services) > bounds.max_services
            ):
                await reject(ws, "unauthorized", "invalid services")
                return
            descriptors = {}
            for descriptor in services:
                name = identifier(descriptor["name"], "service")
                if name in descriptors or (
                    policy.services is not None and name not in policy.services
                ):
                    await reject(ws, "unauthorized", "service not authorized")
                    return
                _validate_descriptor(descriptor, bounds)
                descriptors[name] = descriptor
            conn = ConnectorConnection(agent, descriptors, ws, bounds)
            reg.add(conn)
            await conn.send(
                {"type": "registered", "version": VERSION, "connector_id": conn.connector_id}
            )
            if reg.peers is not None:
                reg.peers.request_start()
            while True:
                async with asyncio.timeout(bounds.idle_timeout):
                    message = decode(await ws.receive_text(), bounds)
                if message == {"type": "heartbeat"}:
                    await conn.send({"type": "heartbeat_ack"})
                    if reg.peers is not None and reg.peers.owner is None:
                        reg.peers.request_start()
                else:
                    conn.dispatch(message)
        except WebSocketDisconnect:
            pass
        except TunnelError as exc:
            if exc.code == "duplicate_service":
                await reject(ws, "duplicate_service", "service already connected")
                return
            await ws.close(code=4400, reason="invalid tunnel frame")
        except (ValueError, TypeError, TimeoutError, KeyError, RuntimeError):
            try:
                await ws.close(code=4400, reason="invalid or expired tunnel frame")
            except RuntimeError:
                pass
        finally:
            if conn is not None:
                reg.remove(conn)

    return connect


def install_connector_fallback(app, deferred):
    """Only an unmatched connector WebSocket may trigger first-use discovery."""
    from starlette._utils import get_route_path

    default = app.router.default
    ingress = None

    async def fallback(scope, receive, send):
        nonlocal ingress
        if scope["type"] == "websocket" and get_route_path(scope) == "/tunnel/connect":
            if ingress is None:
                binding = deferred.for_connector()
                if binding is not None:
                    ingress = _connector_endpoint(
                        {binding.policy.agent_id: binding.policy}, binding.registry, TunnelLimits()
                    )
                    app.state.mcp_tunnel = binding.registry
            if ingress is not None:
                return await ingress(WebSocket(scope, receive, send))
        return await default(scope, receive, send)

    app.router.default = fallback
