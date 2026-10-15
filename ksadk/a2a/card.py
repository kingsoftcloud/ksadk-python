"""AgentCard builder with opt-in public tool producer advertisement."""

from ksadk.a2a.presentation import A2A_TOOL_ACTIVITY_EXTENSION_URI
from ksadk.a2a_card import A2A_PROTOCOL_VERSION, JSONRPC_PATH, REST_PATH_PREFIX
from ksadk.a2a_card import build_agent_card as _build_agent_card


def build_agent_card(*, public_tool_policy=None, **kwargs):
    card = _build_agent_card(**kwargs)
    if public_tool_policy is not None and public_tool_policy.tools:
        from a2a.types import AgentExtension

        card.capabilities.extensions.append(
            AgentExtension(
                uri=A2A_TOOL_ACTIVITY_EXTENSION_URI,
                description="Explicitly allowlisted public tool observations",
                required=False,
            )
        )
    return card


__all__ = ["A2A_PROTOCOL_VERSION", "JSONRPC_PATH", "REST_PATH_PREFIX", "build_agent_card"]
