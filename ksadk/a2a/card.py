"""Compatibility exports for the lightweight shared AgentCard builder."""

from ksadk.a2a_card import (
    A2A_PROTOCOL_VERSION,
    JSONRPC_PATH,
    REST_PATH_PREFIX,
    build_agent_card,
)

__all__ = [
    "A2A_PROTOCOL_VERSION",
    "JSONRPC_PATH",
    "REST_PATH_PREFIX",
    "build_agent_card",
]
