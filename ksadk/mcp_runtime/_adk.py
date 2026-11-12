"""ADK-only MCP types, loaded when constructing an ADK connection."""

from typing import Any

from pydantic import Field

from ksadk.compat.adk_compat import StreamableHTTPConnectionParams


class _RedactedStreamableHTTPConnectionParams(StreamableHTTPConnectionParams):
    """Keep process-scoped MCP connection material out of debug reprs."""

    url: str = Field(repr=False)
    headers: dict[str, Any] | None = Field(default=None, repr=False)
