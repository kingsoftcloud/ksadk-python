"""Trusted business-identity preparation for kernel worker starts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ksadk.conversations.runtime_persistence import ensure_conversation_session
from ksadk.kernel.contracts import AgentControlCommand
from ksadk.sessions.invocation_identity import identity_native_user_id


async def prepare_worker_identity(
    *,
    command: AgentControlCommand,
    defaults: Mapping[str, object],
    session_service: object | None,
) -> tuple[str, Mapping[str, Any] | None]:
    """Resolve the storage user and retain only an admitted identity payload."""

    invocation_identity = command.payload.get("invocation_identity")
    effective_user_id = str(command.tenant_id or "agent-kernel")
    if not isinstance(invocation_identity, Mapping):
        return effective_user_id, None
    if session_service is None:
        return identity_native_user_id(invocation_identity), invocation_identity

    canonical_session = await ensure_conversation_session(
        agent_id=str(defaults.get("agent_id") or command.agent_instance_id),
        user_id=effective_user_id,
        session_id=command.session_id,
        session_service_provider=lambda: session_service,
        invocation_identity=invocation_identity,
    )
    return canonical_session.user_id, invocation_identity
