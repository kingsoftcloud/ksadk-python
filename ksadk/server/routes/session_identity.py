"""Existing Session ownership checks and identity-bound listing."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ksadk.conversations.runtime_persistence import require_conversation_session
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.sessions.invocation_identity import (
    bind_or_validate_session_identity,
    identity_can_adopt_any_legacy_user,
    session_identity_binding_matches,
)

from .projection import _require_action_session


async def _require_identity_action_session(
    service: Any,
    *,
    session_id: str,
    agent_id: str | None,
    user_id: str | None,
    invocation_identity: PlatformIdentityContext,
    history_only: bool = False,
    history_header_loader: Any = None,
):
    if history_only:
        loader = history_header_loader or getattr(
            service, "get_session_header", service.get_session_metadata
        )
        session = await loader(session_id)
        from ksadk.sessions.agent_identity import session_agent_ids

        if (
            session is None
            or (agent_id is not None and session.agent_id not in session_agent_ids(agent_id))
            or (user_id is not None and invocation_identity.is_empty and session.user_id != user_id)
        ):
            raise HTTPException(status_code=404, detail="Session not found")
        if invocation_identity.is_empty:
            return session
        if not agent_id:
            raise HTTPException(status_code=400, detail="AgentId is required")
        from ksadk.sessions.invocation_identity import bind_or_validate_session_identity

        return await bind_or_validate_session_identity(
            service=service,
            session=session,
            identity=invocation_identity,
            requested_user_id=str(user_id or ""),
        )
    if invocation_identity.is_empty:
        return await _require_action_session(
            service,
            session_id=session_id,
            agent_id=agent_id,
            user_id=user_id,
        )
    if not agent_id:
        raise HTTPException(status_code=400, detail="AgentId is required")
    return await require_conversation_session(
        agent_id=agent_id,
        user_id=str(user_id or ""),
        session_id=session_id,
        session_service_provider=lambda: service,
        invocation_identity=invocation_identity,
    )


async def _list_identity_session_metadata(
    service: Any,
    *,
    agent_id: str,
    user_id: str | None,
    invocation_identity: PlatformIdentityContext,
) -> list[Any]:
    """List bound sessions; only the verified IAM account may adopt legacy rows."""

    if invocation_identity.is_empty:
        return await service.list_session_metadata(agent_id, user_id)
    from ksadk.sessions.agent_identity import session_agent_ids
    from ksadk.sessions.invocation_identity import identity_native_user_id

    candidate_users: set[str | None] = {identity_native_user_id(invocation_identity)}
    if identity_can_adopt_any_legacy_user(invocation_identity):
        candidate_users.add(None)
    elif (
        invocation_identity.identity_namespace == "kscloud-iam"
        and invocation_identity.subject_type == "user"
    ):
        candidate_users.add(invocation_identity.subject_id)
    sessions: list[Any] = []
    seen: set[str] = set()
    for candidate_user in candidate_users:
        candidates = [
            session
            for owner_id in session_agent_ids(agent_id)
            for session in await service.list_session_metadata(owner_id, candidate_user)
        ]
        for session in candidates:
            if session.id in seen:
                continue
            matches = await session_identity_binding_matches(
                service=service,
                session=session,
                identity=invocation_identity,
            )
            if not matches:
                try:
                    await bind_or_validate_session_identity(
                        service=service,
                        session=session,
                        identity=invocation_identity,
                        requested_user_id=session.user_id,
                    )
                except HTTPException:
                    matches = False
                else:
                    matches = True
            if matches:
                sessions.append(session)
                seen.add(session.id)
    sessions.sort(
        key=lambda item: (item.updated_at, item.created_at, item.id),
        reverse=True,
    )
    return sessions
