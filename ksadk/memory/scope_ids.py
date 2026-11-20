"""AICP wire identity encoding, separate from persisted MemoryProvider scopes."""

from __future__ import annotations

import hashlib
import re

SAFE_SCOPE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SCOPE_ID_SEPARATOR = "_"
SCOPE_ID_MAX_READABLE = 60
SCOPE_ID_DIGEST_CHARS = 32


def aicp_agent_user_scope_id(*, agent_id: str, user_id: str) -> str:
    """Encode Agent × User using the upstream readable/digest wire format.

    This encoding belongs to the AICP boundary. Local records keep the stable
    logical namespace returned by ``coordinator.agent_user_scope_id``.
    """
    agent = str(agent_id or "").strip()
    user = str(user_id or "").strip()
    if not agent and SAFE_SCOPE_ID.fullmatch(user):
        return user
    if agent and SCOPE_ID_SEPARATOR not in agent and SCOPE_ID_SEPARATOR not in user:
        candidate = f"{agent}{SCOPE_ID_SEPARATOR}{user}"
        if len(candidate) <= SCOPE_ID_MAX_READABLE and SAFE_SCOPE_ID.fullmatch(candidate):
            return candidate
    encoded = f"agent-user-scope/v1|{agent}|{user}".encode()
    return hashlib.sha256(encoded).hexdigest()[:SCOPE_ID_DIGEST_CHARS]


def aicp_user_id(logical_id: str) -> str:
    """Preserve existing valid cloud IDs and encode legacy logical namespaces.

    In particular, do not rehash resource-runtime ``mp1-`` partitions or safe
    caller-provided IDs: they can already own records in the cloud service.
    """
    value = str(logical_id or "")
    if not value.strip():
        raise ValueError("MEMORY_USER_ID_REQUIRED")
    if SAFE_SCOPE_ID.fullmatch(value):
        return value
    if value.startswith("agent:"):
        agent, separator, user = value[len("agent:"):].partition(":user:")
        if separator:
            if not agent.strip() or not user.strip():
                raise ValueError("MEMORY_USER_ID_REQUIRED")
            return aicp_agent_user_scope_id(agent_id=agent, user_id=user)
    return aicp_agent_user_scope_id(agent_id="", user_id=value)
