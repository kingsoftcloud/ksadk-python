"""Per-application registry for connectors and their public service descriptors."""

from __future__ import annotations

import logging
import secrets
import threading
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from ksadk.tunnel.server import ConnectorConnection

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOLS = frozenset({"mcp"})


class ServerDescriptor(BaseModel):
    """Public description of a service exposed through a connector.

    Local addresses, credentials, and query parameters deliberately have no
    representation in this model and never leave the connector process.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    protocol: str = "mcp"
    tool_filter: list[str] | None = None

    @field_validator("name", "protocol")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("descriptor names and protocols must be non-empty strings")
        return value.strip()

    @field_validator("tool_filter")
    @classmethod
    def _valid_tool_filter(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("tool_filter must contain only non-empty strings")
        return [item.strip() for item in value]


def validate_registration(
    agent_name: Any,
    servers: Any,
) -> tuple[str, list[ServerDescriptor]]:
    """Validate and detach a complete registration before changing registry state."""

    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("agent must be a non-empty string")
    if not isinstance(servers, list) or not servers:
        raise ValueError("servers must be a non-empty list")

    descriptors: list[ServerDescriptor] = []
    seen: set[str] = set()
    for raw in servers:
        descriptor = (
            raw.model_copy(deep=True)
            if isinstance(raw, ServerDescriptor)
            else ServerDescriptor.model_validate(raw)
        )
        if descriptor.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"unsupported tunnel protocol: {descriptor.protocol}")
        if descriptor.name in seen:
            raise ValueError(f"duplicate server name in registration: {descriptor.name}")
        seen.add(descriptor.name)
        descriptors.append(descriptor)
    return agent_name.strip(), descriptors


class TunnelRegistry:
    """Own connector routes for one application instance.

    The standard runtime injects the same instance into the server and toolset.
    No module-global registry is consulted when an instance is explicitly given.
    """

    def __init__(self) -> None:
        self.proxy_token = secrets.token_urlsafe(32)
        self.self_url: str | None = None
        # The automatic Toolset is app-owned, just like this registry. Per-run
        # ADKRunner instances attach it but never close it.
        self.toolset: Any | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._connections: dict[str, ConnectorConnection] = {}
        self._routes: dict[tuple[str, str], tuple[str, ServerDescriptor]] = {}

    def claim_tunnel_toolset(self, candidate: Any) -> Any:
        """Atomically select one app-owned Toolset for concurrent runner loads."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Tunnel registry is closed")
            if self.toolset is None:
                self.toolset = candidate
            return self.toolset

    def add_connection(self, conn: ConnectorConnection) -> None:
        """Atomically add a connector, rejecting all conflicting service names."""

        if not isinstance(conn.connector_id, str) or not conn.connector_id:
            raise ValueError("connector_id must be a non-empty string")
        agent_name, descriptors = validate_registration(conn.agent_name, conn.servers)

        with self._lock:
            if self._closed:
                raise RuntimeError("Tunnel registry is closed")
            if conn.connector_id in self._connections:
                raise ValueError("connector id is already registered")
            for descriptor in descriptors:
                key = (agent_name, descriptor.name)
                if key in self._routes:
                    raise ValueError(
                        f"server {descriptor.name!r} is already registered for this agent"
                    )

            conn.agent_name = agent_name
            conn.servers = descriptors
            self._connections[conn.connector_id] = conn
            for descriptor in descriptors:
                self._routes[(agent_name, descriptor.name)] = (
                    conn.connector_id,
                    descriptor,
                )

        logger.info(
            "Connector %s registered for agent %s with servers %s",
            conn.connector_id,
            agent_name,
            [descriptor.name for descriptor in descriptors],
        )

    def remove_connection(
        self,
        connector_id: str,
        connection: ConnectorConnection | None = None,
    ) -> None:
        """Remove only state still owned by the specified connection."""

        with self._lock:
            current = self._connections.get(connector_id)
            if current is None or (connection is not None and current is not connection):
                return
            self._connections.pop(connector_id, None)
            for key, (owner_id, _) in list(self._routes.items()):
                if owner_id == connector_id:
                    self._routes.pop(key, None)

        logger.info(
            "Connector %s removed from agent %s",
            connector_id,
            current.agent_name,
        )

    def list_server_routes(self, agent_name: str) -> list[tuple[str, ServerDescriptor]]:
        """Return ``(connector_id, descriptor)`` pairs for handler ownership tracking."""

        with self._lock:
            routes = [
                (owner_id, descriptor)
                for (agent, _), (owner_id, descriptor) in sorted(self._routes.items())
                if agent == agent_name
            ]
            return [(owner_id, descriptor.model_copy(deep=True)) for owner_id, descriptor in routes]

    def list_servers(self, agent_name: str) -> list[ServerDescriptor]:
        """Return detached public descriptors for one agent in stable order."""

        return [descriptor for _, descriptor in self.list_server_routes(agent_name)]

    def find_connection(
        self,
        agent_name: str,
        server_name: str,
    ) -> ConnectorConnection | None:
        """Resolve the single connector owning ``(agent_name, server_name)``."""

        with self._lock:
            route = self._routes.get((agent_name, server_name))
            if route is None:
                return None
            return self._connections.get(route[0])

    async def close(self) -> None:
        """Close the app-owned Toolset and fail all active connector requests once."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections.values())
            self._connections.clear()
            self._routes.clear()
            toolset = self.toolset

        for connection in connections:
            connection.fail_all()
        if toolset is not None:
            await toolset.close()

    def has_agent(self, agent_name: str) -> bool:
        with self._lock:
            return any(conn.agent_name == agent_name for conn in self._connections.values())


_REGISTRY: TunnelRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> TunnelRegistry:
    """Return the embedded fallback registry; standard runtime injects its own."""

    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = TunnelRegistry()
    return _REGISTRY


__all__ = [
    "SUPPORTED_PROTOCOLS",
    "ServerDescriptor",
    "TunnelRegistry",
    "get_registry",
    "validate_registration",
]
