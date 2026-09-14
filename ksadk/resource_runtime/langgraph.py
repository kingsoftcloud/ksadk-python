"""Explicit LangGraph tools backed by the existing Core's scoped MCP endpoint.

The trusted runner supplies admitted leases and owns this async context for the
whole graph invocation. Never put the connector, leases or session in graph
state/checkpoints. This does not mutate compiled graphs or supply memory hooks.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

import httpx
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ksadk.plugins.providers.dsh_capabilities import DshMcpConnectorLease
from ksadk.resource_runtime.leases import ResourceLease


@asynccontextmanager
async def create_bound_resource_tools(
    connector: DshMcpConnectorLease,
    leases: Sequence[ResourceLease],
    *,
    tool_aliases: Mapping[str, str],
) -> AsyncIterator[tuple[BaseTool, ...]]:
    """Open scoped tools for explicit graph factory/ToolNode/model.bind_tools use.

    Uses the installed MCP SDK transport and LangChain tool contract. Closing the
    context invalidates escaped tools. Renewal requires reopening at a safe graph
    boundary with fresh host-issued leases; this helper cannot grant permissions.
    Writes still require the broker's host approval/event authorizer.
    """
    aliases = dict(tool_aliases)
    token = connector.resource_bearer_token(aliases, leases)
    opened = False
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(60, connect=10),
    ) as client:
        async with streamable_http_client(connector.endpoint, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                inventory = await session.list_tools()
                by_name = {tool.name: tool for tool in inventory.tools}
                if (
                    inventory.nextCursor
                    or len(by_name) != len(inventory.tools)
                    or set(by_name) != set(aliases)
                ):
                    raise RuntimeError("RESOURCE_TOOL_INVENTORY_MISMATCH")
                opened = True
                tools = []

                def make_tool(name):
                    descriptor = by_name[name]

                    async def invoke(**arguments):
                        if not opened:
                            raise ToolException("RESOURCE_SESSION_CLOSED")
                        try:
                            result = await session.call_tool(name, arguments)
                        except Exception:
                            raise ToolException("RESOURCE_MCP_CALL_FAILED") from None
                        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if result.isError:
                            # Preserve structured diagnostics and tell ToolNode the
                            # call failed. Never return a failed business call as success.
                            raise ToolException(json.dumps(payload, ensure_ascii=False))
                        content = "\n".join(
                            item.text for item in result.content if item.type == "text"
                        )
                        return content, payload

                    return StructuredTool.from_function(
                        coroutine=invoke,
                        name=name,
                        description=descriptor.description or name,
                        args_schema=descriptor.inputSchema,
                        infer_schema=False,
                        response_format="content_and_artifact",
                        handle_tool_error=True,
                    )

                try:
                    for name in aliases:
                        tools.append(make_tool(name))
                    yield tuple(tools)
                finally:
                    opened = False
