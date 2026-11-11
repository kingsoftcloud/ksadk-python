"""Dynamic ADK toolset backed by the current application's tunnel registry."""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote

from ksadk.compat.adk_compat import BaseTool, BaseToolset, ReadonlyContext
from ksadk.tunnel.protocol import get_protocol
from ksadk.tunnel.protocol.base import BaseProtocol
from ksadk.tunnel.registry import TunnelRegistry, get_registry

logger = logging.getLogger(__name__)


class TunnelToolset(BaseToolset):
    """Refresh protocol handlers as online services and descriptors change."""

    def __init__(
        self,
        agent_name: str,
        registry: TunnelRegistry | None = None,
        self_url: str | None = None,
    ) -> None:
        super().__init__()
        self.agent_name = agent_name
        self._registry = registry if registry is not None else get_registry()
        self._self_url = self_url
        self._handlers: dict[str, BaseProtocol] = {}
        self._handler_owners: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def registry(self) -> TunnelRegistry:
        return self._registry

    @property
    def closed(self) -> bool:
        return self._closed

    def _self_base_url(self) -> str:
        if self._self_url is not None:
            return self._self_url.rstrip("/")
        if self._registry.self_url is not None:
            return self._registry.self_url.rstrip("/")
        configured = os.environ.get("KSADK_TUNNEL_SELF_URL")
        if configured:
            return configured.rstrip("/")
        return f"http://127.0.0.1:{os.environ.get('PORT') or '8000'}"

    async def get_tools(
        self,
        readonly_context: ReadonlyContext | None = None,
    ) -> list[BaseTool]:
        async with self._lock:
            if self._closed:
                return []
            routes = self._registry.list_server_routes(self.agent_name)
            online = {server.name: (owner_id, server) for owner_id, server in routes}

            for stale in set(self._handlers) - set(online):
                handler = self._handlers.pop(stale)
                self._handler_owners.pop(stale, None)
                await self._safe_close(stale, handler)

            for name, (owner_id, server) in online.items():
                existing = self._handlers.get(name)
                owner_changed = self._handler_owners.get(name) != owner_id
                if existing is not None and (existing.descriptor != server or owner_changed):
                    self._handlers.pop(name, None)
                    self._handler_owners.pop(name, None)
                    await self._safe_close(name, existing)
                    existing = None
                if existing is None:
                    proxy_url = (
                        f"{self._self_base_url()}/tunnel/mcp/"
                        f"{quote(self.agent_name, safe='')}/{quote(name, safe='')}"
                    )
                    try:
                        existing = get_protocol(server.protocol)(
                            server,
                            proxy_url,
                            self._registry.proxy_token,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Could not create tunnel handler for agent %s server %s (%s)",
                            self.agent_name,
                            name,
                            type(exc).__name__,
                        )
                        continue
                    self._handlers[name] = existing
                    self._handler_owners[name] = owner_id

            tools: list[BaseTool] = []
            for name, handler in self._handlers.items():
                try:
                    tools.extend(await handler.get_tools(readonly_context))
                except Exception as exc:
                    logger.warning(
                        "Could not load tunnel tools for agent %s server %s (%s)",
                        self.agent_name,
                        name,
                        type(exc).__name__,
                    )
            return tools

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            handlers = list(self._handlers.items())
            self._handlers.clear()
            self._handler_owners.clear()
            for name, handler in handlers:
                await self._safe_close(name, handler)

    async def _safe_close(self, name: str, handler: BaseProtocol) -> None:
        try:
            await handler.close()
        except Exception as exc:
            logger.warning(
                "Could not close tunnel handler for agent %s server %s (%s)",
                self.agent_name,
                name,
                type(exc).__name__,
            )


__all__ = ["TunnelToolset"]
