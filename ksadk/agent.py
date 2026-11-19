"""Declare runtime features on the exported framework agent."""

import re
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class TunnelDeclaration:
    enabled: bool
    token_env: str


def Agent(
    agent: T, *, enable_tunnel: bool = False, tunnel_token_env: str = "KSADK_TUNNEL_TOKEN"
) -> T:
    """Return the original framework object; the runtime reads the secret at startup."""
    if not isinstance(enable_tunnel, bool):
        raise TypeError("enable_tunnel must be a bool")
    if not isinstance(tunnel_token_env, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z_0-9]*", tunnel_token_env
    ):
        raise ValueError("tunnel_token_env must be an environment variable name")
    object.__setattr__(
        agent, "_ksadk_tunnel_declaration", TunnelDeclaration(enable_tunnel, tunnel_token_env)
    )
    return agent
