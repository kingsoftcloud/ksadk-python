"""Cloud-side tool operations over the Connector's streaming HTTP transport."""

from __future__ import annotations

import json
from http import HTTPStatus

# The transport, response streams and MCP client must use the same backend,
# even when both httpx packages are installed in a legacy MCP environment.
from ksadk._mcp_compat import httpx_module

httpx = httpx_module()
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent

from ksadk._mcp_compat import field, list_tools_page
from ksadk._mcp_compat import streamable_http

from .protocol import TunnelError


class TunnelTransport(httpx.AsyncBaseTransport):
    def __init__(self, connection, service):
        self.connection = connection
        self.service = service

    async def handle_async_request(self, request):
        return await self.connection.forward(self.service, request)

    def client(self, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=self, headers=headers, timeout=timeout, auth=auth)


async def api_operation(transport, descriptor, name, arguments):
    tools = descriptor["tools"]
    if name is None:
        return {"tools": tools}
    if name not in {tool["name"] for tool in tools}:
        raise TunnelError("forbidden")
    async with transport.client() as client:
        response = await client.post(f"http://tunnel/{name}", json=arguments)
        status = response.status_code
        if (
            response.is_client_error
            and status in HTTPStatus
            and status != HTTPStatus.REQUEST_TIMEOUT
        ):
            # A completed client-error response reaches the model as a tool result.
            # Error bodies can contain internal diagnostics and credentials.
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(type="text", text=f"HTTP {status} {HTTPStatus(status).phrase}")
                ],
                structuredContent={"status_code": status},
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
        response.raise_for_status()
        media_type = response.headers.get("content-type", "").partition(";")[0].lower()
        value = None
        if response.content and (media_type == "application/json" or media_type.endswith("+json")):
            value = json.loads(response.content)
        return CallToolResult(
            content=[TextContent(type="text", text=response.text)] if response.content else [],
            structuredContent=value if isinstance(value, dict) else None,
        ).model_dump(by_alias=True, mode="json", exclude_none=True)


async def mcp_operation(transport, descriptor, name, arguments):
    allowed = descriptor.get("tool_filter")
    if name is not None and allowed is not None and name not in allowed:
        raise TunnelError("forbidden")
    limits = transport.connection.limits
    async with (
        streamable_http(
            "http://tunnel/", httpx_client_factory=transport.client, timeout=limits.request_timeout
        ) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        if name is not None:
            result = await session.call_tool(name, arguments)
            return result.model_dump(by_alias=True, mode="json", exclude_none=True)
        tools, cursors = [], set()
        cursor = None
        while True:
            page = await list_tools_page(session, cursor)
            tools.extend(page.tools)
            cursor = field(page, "next_cursor", "nextCursor")
            if len(tools) > limits.max_tools or cursor in cursors:
                raise ValueError("invalid MCP catalog")
            if not cursor:
                break
            cursors.add(cursor)
        return {
            "tools": [
                t.model_dump(by_alias=True, mode="json", exclude_none=True)
                for t in tools
                if allowed is None or t.name in allowed
            ]
        }
