"""Protocol handler lookup for tunneled services."""

from __future__ import annotations

from ksadk.tunnel.protocol.base import BaseProtocol
from ksadk.tunnel.protocol.mcp import McpProtocol

PROTOCOLS: dict[str, type[BaseProtocol]] = {McpProtocol.type: McpProtocol}


def get_protocol(protocol_type: str) -> type[BaseProtocol]:
    """Return the handler for a validated protocol discriminator."""

    try:
        return PROTOCOLS[protocol_type]
    except KeyError as exc:
        raise ValueError(f"unsupported tunnel protocol: {protocol_type}") from exc


__all__ = ["BaseProtocol", "McpProtocol", "PROTOCOLS", "get_protocol"]
