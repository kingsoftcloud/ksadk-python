"""Base contract for turning a tunneled service into ADK tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ksadk.compat.adk_compat import BaseTool, ReadonlyContext
    from ksadk.tunnel.registry import ServerDescriptor


class BaseProtocol(ABC):
    """Handle one online service descriptor."""

    type: ClassVar[str]

    def __init__(
        self,
        descriptor: ServerDescriptor,
        proxy_base_url: str,
        proxy_token: str,
    ) -> None:
        self.descriptor = descriptor
        self.proxy_base_url = proxy_base_url
        self._proxy_token = proxy_token

    @abstractmethod
    async def get_tools(
        self,
        readonly_context: ReadonlyContext | None = None,
    ) -> list[BaseTool]:
        """Return this service's tools for the current agent turn."""

    async def close(self) -> None:
        """Release resources held by this handler."""
