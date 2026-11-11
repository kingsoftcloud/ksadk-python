"""Streamable HTTP MCP over the authenticated in-process tunnel proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ksadk.mcp_runtime import MCPServerConfig, build_mcp_toolset
from ksadk.tunnel.protocol.base import BaseProtocol
from ksadk.tunnel.registry import ServerDescriptor

if TYPE_CHECKING:
    from ksadk.compat.adk_compat import BaseTool, ReadonlyContext


class McpProtocol(BaseProtocol):
    """Expose a tunneled Streamable HTTP MCP server through the ADK MCP runtime."""

    type = "mcp"

    def __init__(
        self,
        descriptor: ServerDescriptor,
        proxy_base_url: str,
        proxy_token: str,
    ) -> None:
        super().__init__(descriptor, proxy_base_url, proxy_token)
        self._config = MCPServerConfig(
            name=descriptor.name,
            url=proxy_base_url,
            api_key=proxy_token,
            tool_filter=tuple(descriptor.tool_filter or ()),
        )
        self._toolset: Any | None = None

    def _ensure_toolset(self) -> Any:
        if self._toolset is None:
            self._toolset = build_mcp_toolset(self._config)
        return self._toolset

    async def get_tools(
        self,
        readonly_context: ReadonlyContext | None = None,
    ) -> list[BaseTool]:
        return cast("list[BaseTool]", await self._ensure_toolset().get_tools(readonly_context))

    async def close(self) -> None:
        toolset, self._toolset = self._toolset, None
        if toolset is not None:
            await toolset.close()


__all__ = ["McpProtocol"]
