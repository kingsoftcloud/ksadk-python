"""Framework-independent discovery and connector-generation-bound tool handles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

from mcp.types import CallToolResult, ListToolsResult, Tool

from .protocol import IncompletePeerDiscovery, TunnelError
from .registry import TunnelRegistry

logger = logging.getLogger(__name__)


def tool_name(service: str, name: str) -> str:
    raw = f"tunnel_{service}__{name}"
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    digest = hashlib.sha256(json.dumps([service, name]).encode()).hexdigest()[:16]
    return safe[:47] + "_" + digest


@dataclass(frozen=True)
class TunnelToolHandle:
    agent_id: str
    service: str
    connector_id: str
    descriptor: Tool
    registry: TunnelRegistry = field(repr=False)
    owner: dict | None = None

    @property
    def name(self) -> str:
        return tool_name(self.service, self.descriptor.name)

    async def call(self, arguments: dict) -> dict:
        result = await self.registry.call_tool(
            self.agent_id,
            self.service,
            self.descriptor.name,
            arguments,
            connector_id=self.connector_id,
            **({"owner": self.owner} if self.owner is not None else {}),
        )
        try:
            return CallToolResult.model_validate(result).model_dump(by_alias=True, mode="json", exclude_none=True)
        except ValueError:
            raise TunnelError("invalid_result", indeterminate=True) from None

    def snapshot(self) -> dict:
        """Only public descriptors and generation IDs enter graph state."""
        return {
            "name": self.name,
            "service": self.service,
            "connector_id": self.connector_id,
            "tool": self.descriptor.model_dump(mode="json", exclude_none=True),
            **({"owner": self.owner} if self.owner is not None else {}),
        }


async def discover_tools(agent_id: str, registry: TunnelRegistry) -> list[TunnelToolHandle]:
    async def discover(service, connection):
        try:
            result = await registry.list_tools(
                agent_id,
                service,
                connector_id=connection.connector_id,
                **(
                    {"owner": connection.owner}
                    if getattr(connection, "owner", None) is not None
                    else {}
                ),
            )
            tools = ListToolsResult.model_validate(result).tools
            if len(tools) > connection.limits.max_tools or len(
                {tool.name for tool in tools}
            ) != len(tools):
                raise ValueError("invalid tool list")
            return [
                TunnelToolHandle(
                    agent_id,
                    service,
                    connection.connector_id,
                    tool,
                    registry,
                    getattr(connection, "owner", None),
                )
                for tool in tools
            ]
        except TunnelError as exc:
            logger.warning("MCP tunnel discovery failed (%s)", exc.code)
            return []
        except ValueError:
            logger.warning("MCP tunnel returned an invalid tool list")
            return []

    try:
        connections = await registry.discover_connections(agent_id)
    except IncompletePeerDiscovery:
        # The model can still chat and use its non-tunnel tools during a rollout.
        return []
    batches = await asyncio.gather(*(discover(service, conn) for service, conn in connections))
    return [tool for tools in batches for tool in tools]
