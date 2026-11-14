"""Explicit LangChain tools backed by the current application's Tunnel registry."""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Sequence
from urllib.parse import quote

from ksadk.mcp_runtime import MCPServerConfig, build_langchain_connection
from ksadk.tunnel.config import TunnelConfig, default_tunnel_self_url, tunnel_enabled
from ksadk.tunnel.registry import TunnelRegistry

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Binding:
    agent_name: str
    registry: TunnelRegistry


_binding: ContextVar[_Binding | None] = ContextVar("ksadk_langgraph_tunnel", default=None)


@contextmanager
def bind_langgraph_tunnel(config: TunnelConfig, registry: TunnelRegistry) -> Iterator[None]:
    """Bind trusted process-only dependencies for one complete graph execution."""
    token = _binding.set(_Binding(config.agent_name, registry))
    try:
        yield
    finally:
        _binding.reset(token)


@contextmanager
def langgraph_tunnel_execution(runner: Any) -> Iterator[None]:
    """Bind the current runner's app-owned dependencies for one native operation."""
    config = getattr(runner, "_tunnel_config", None)
    if config is None:
        yield
        return
    registry = getattr(runner, "_tunnel_registry", None)
    if registry is None:
        raise RuntimeError("LangGraph Tunnel requires an app-owned registry")
    with bind_langgraph_tunnel(config, registry):
        yield


def _load_mcp_tools():
    try:
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError as exc:
        raise ImportError(
            "LangGraph Tunnel requires langchain-mcp-adapters>=0.3.2; "
            "install the documented KsADK LangGraph dependencies"
        ) from exc
    return load_mcp_tools


def _model_name(value: str) -> str:
    """Keep ordinary names legible and bound unusual names for model APIs."""
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", value) and "__" not in value:
        return value
    slug = re.sub(r"[^a-zA-Z0-9-]", "_", value)[:12].strip("_") or "tool"
    return slug + "_" + hashlib.sha256(value.encode()).hexdigest()[:10]


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


def _append_unique(tools: list[Any], seen: set[str], tool: Any) -> None:
    name = _tool_name(tool)
    if not name:
        raise ValueError("LangGraph Tunnel tools must have non-empty names")
    if name in seen:
        raise ValueError(f"Duplicate LangGraph tool name after Tunnel merging: {name!r}")
    seen.add(name)
    tools.append(tool)


def _namespaced_tool(tool: BaseTool, server_name: str) -> BaseTool:
    name = f"{_model_name(server_name)}__{_model_name(tool.name)}"
    if tool.name == name:
        return tool
    return tool.model_copy(update={"name": name})


class LangGraphTunnelTools:
    """Resolve current Tunnel routes into LangChain tools without mutating a graph."""

    def __init__(
        self,
        agent_name: str,
        registry: TunnelRegistry | None = None,
        self_url: str | None = None,
    ) -> None:
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("LangGraphTunnelTools requires a non-empty agent_name")
        self.agent_name = agent_name.strip()
        self._registry = registry
        self._self_url = self_url.rstrip("/") if self_url else None

    def _resolve(self) -> tuple[TunnelRegistry, str] | None:
        binding = _binding.get()
        if binding is not None and binding.agent_name != self.agent_name:
            raise ValueError(
                f"LangGraph Tunnel Agent mismatch: helper={self.agent_name!r}, "
                f"runtime={binding.agent_name!r}"
            )
        if self._registry is not None:
            if binding is not None and binding.registry is not self._registry:
                raise ValueError("Manual and app-owned Tunnel registries must not be mixed")
            self_url = self._self_url or self._registry.self_url
            return self._registry, self_url or default_tunnel_self_url()
        if binding is None:
            if tunnel_enabled():
                raise RuntimeError(
                    "LangGraph Tunnel requires an active KsADK runtime execution context"
                )
            return None
        self_url = self._self_url or binding.registry.self_url
        return binding.registry, self_url or default_tunnel_self_url()

    async def get_tools(self, *, tools: Sequence[BaseTool] = ()) -> list[BaseTool]:
        """Return existing tools plus the currently discoverable Tunnel tools."""
        merged: list[BaseTool] = []
        seen: set[str] = set()
        for tool in tools:
            _append_unique(merged, seen, tool)

        resolved = self._resolve()
        if resolved is None:
            return merged
        registry, self_url = resolved
        routes = registry.list_server_routes(self.agent_name)
        if not routes:
            return merged

        load_mcp_tools = _load_mcp_tools()
        for _owner_id, server in routes:
            proxy_url = (
                f"{self_url.rstrip('/')}/tunnel/mcp/"
                f"{quote(self.agent_name, safe='')}/{quote(server.name, safe='')}"
            )
            try:
                config = MCPServerConfig(
                    name=server.name,
                    url=proxy_url,
                    api_key=registry.proxy_token,
                    tool_filter=tuple(server.tool_filter or ()),
                )
                discovered = await load_mcp_tools(
                    None,
                    connection=build_langchain_connection(config),
                    server_name=server.name,
                )
            except Exception as exc:
                logger.warning(
                    "Could not load LangGraph tunnel tools for agent %s server %s (%s)",
                    self.agent_name,
                    server.name,
                    type(exc).__name__,
                )
                continue

            allowed = set(server.tool_filter or ())
            for tool in discovered:
                if allowed and tool.name not in allowed:
                    continue
                _append_unique(merged, seen, _namespaced_tool(tool, server.name))
        return merged


__all__ = ["LangGraphTunnelTools"]
