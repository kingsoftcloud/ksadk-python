"""Explicit LangGraph tools with serializable discovery snapshots and no ADK imports."""

from __future__ import annotations

from ksadk._mcp_compat import tool_input_schema

import json

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from mcp.types import Tool
from pydantic import PrivateAttr

from .discovery import TunnelToolHandle, discover_tools
from .protocol import TunnelError, identifier
from .registry import TunnelRegistry


class _TunnelLangChainTool(BaseTool):
    """Keep native tool callbacks and MCP error/artifact semantics together."""

    _handle: TunnelToolHandle = PrivateAttr()

    def __init__(self, handle: TunnelToolHandle):
        super().__init__(
            name=handle.name,
            description=handle.descriptor.description or "",
            args_schema=tool_input_schema(handle.descriptor),
        )
        self._handle = handle

    def _to_args_and_kwargs(self, tool_input, tool_call_id):
        _, arguments = super()._to_args_and_kwargs(tool_input, tool_call_id)
        # Pass the native ToolCall identity outside the MCP argument namespace.
        return (arguments, tool_call_id), {}

    def _run(self, *args, **kwargs):
        raise NotImplementedError("MCP tunnel tools require async ainvoke")

    async def _arun(self, arguments, call_id):
        result = await self._handle.call(arguments)
        if call_id is None:
            return result
        return ToolMessage(
            name=self.name,
            tool_call_id=call_id,
            content=json.dumps(result, ensure_ascii=False),
            artifact=result,
            status="error" if result.get("isError") else "success",
        )


class TunnelTools:
    """App-owned tools for model and ToolNode integration in caller-owned graphs.

    A model node calls ``catalog = await tunnel_tools.discover()`` and saves
    that JSON catalog in graph state alongside its AIMessage. Bind tools from
    ``await tunnel_tools.get_tools(catalog)`` in the model node, and pass tools
    from that same catalog to the execution node. This preserves the original
    connector generation between model selection and execution. No clients,
    closures or credentials enter graph state; never cache a catalog across turns.
    """

    def __init__(self, agent_id: str, registry: TunnelRegistry):
        self.agent_id = identifier(agent_id, "agent_id")
        self._registry = registry

    async def discover(self) -> list[dict]:
        return [tool.snapshot() for tool in await discover_tools(self.agent_id, self._registry)]

    def _handles(self, catalog: list[dict]) -> list[TunnelToolHandle]:
        if not isinstance(catalog, list):
            raise ValueError("tunnel catalog must be a list")
        handles, names = [], set()
        for entry in catalog:
            if not isinstance(entry, dict) or set(entry) - {"owner"} != {
                "name",
                "service",
                "connector_id",
                "tool",
            }:
                raise ValueError("invalid tunnel catalog entry")
            handle = TunnelToolHandle(
                self.agent_id,
                identifier(entry["service"], "service"),
                identifier(entry["connector_id"], "connector_id"),
                Tool.model_validate(entry["tool"]),
                self._registry,
                entry.get("owner"),
            )
            if handle.owner is not None:
                from .peers import validate_owner

                validate_owner(handle.owner)
            if handle.name != entry["name"] or handle.name in names:
                raise ValueError("invalid or duplicate tunnel tool name")
            names.add(handle.name)
            handles.append(handle)
        return handles

    def model_tools(self, catalog: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.descriptor.description or "",
                    "parameters": tool_input_schema(tool.descriptor),
                },
            }
            for tool in self._handles(catalog)
        ]

    async def call_tool(self, catalog: list[dict], name: str, arguments: dict) -> dict:
        for tool in self._handles(catalog):
            if tool.name == name:
                return await tool.call(arguments)
        raise TunnelError("tool_unavailable")

    async def get_tools(self, catalog: list[dict] | None = None) -> list[BaseTool]:
        """Return LangChain tools; absent catalog discovers current online tools."""
        if catalog is None:
            catalog = await self.discover()

        return [_TunnelLangChainTool(tool) for tool in self._handles(catalog)]
