"""ADK integration, loaded only when the application requests ADK tools."""

from __future__ import annotations

from ksadk._mcp_compat import tool_input_schema

from typing import Any

from ksadk.compat.adk_compat import BaseTool, BaseToolset
from ksadk.compat.adk_compat import genai_types as types

from .discovery import TunnelToolHandle, discover_tools
from .discovery import tool_name as _tool_name
from .protocol import identifier
from .registry import TunnelRegistry


class TunnelTool(BaseTool):
    def __init__(self, *, agent_id, service, connector_id, descriptor, registry, owner=None):
        super().__init__(
            name=_tool_name(service, descriptor.name), description=descriptor.description or ""
        )
        self._agent_id = agent_id
        self._service = service
        self._connector_id = connector_id
        self._descriptor = descriptor
        self._registry = registry
        self._owner = owner

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=tool_input_schema(self._descriptor),
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> dict:
        return await TunnelToolHandle(
            self._agent_id,
            self._service,
            self._connector_id,
            self._descriptor,
            self._registry,
            self._owner,
        ).call(args)


class TunnelToolset(BaseToolset):
    """Read the app registry every ADK discovery; no loopback HTTP or tool cache."""

    def __init__(self, agent_id: str, registry: TunnelRegistry):
        super().__init__()
        self.agent_id = identifier(agent_id, "agent_id")
        self._registry = registry
        self._closed = False
        # New ADK releases optionally cache an invocation; the tunnel deliberately
        # re-reads online registrations on every discovery in both ADK versions.
        if hasattr(self, "_use_invocation_cache"):
            self._use_invocation_cache = False

    async def get_tools(self, readonly_context=None):
        if self._closed:
            return []

        return [
            TunnelTool(
                agent_id=self.agent_id,
                service=tool.service,
                connector_id=tool.connector_id,
                descriptor=tool.descriptor,
                registry=self._registry,
                owner=tool.owner,
            )
            for tool in await discover_tools(self.agent_id, self._registry)
        ]

    async def close(self):
        self._closed = True
