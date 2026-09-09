"""Trusted business-identity binding for canonical KsADK sessions.

The public ``UserId`` field remains a compatibility hint.  Hosted callers are
authorized by a verified four-part business identity, which is converted to an
opaque native user key and durably bound to the session before transcript
access.  This prevents a known ``SessionId`` from becoming an authorization
credential while still allowing an authorized legacy session to be adopted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from ksadk.runtime_context import PlatformIdentityContext
from ksadk.sessions.base import BaseSessionService, Session

SESSION_IDENTITY_SCOPE = "platform_identity"
SESSION_IDENTITY_BINDING_VERSION = 1
DEFAULT_IAM_IDENTITY_NAMESPACE = "kscloud-iam"


def coerce_platform_identity(value: Any) -> PlatformIdentityContext:
    if isinstance(value, PlatformIdentityContext):
        return value
    return PlatformIdentityContext.from_payload(value if isinstance(value, Mapping) else None)


def require_complete_platform_identity(value: Any) -> PlatformIdentityContext:
    identity = coerce_platform_identity(value)
    if identity.is_empty:
        return identity
    if not identity.is_complete:
        raise HTTPException(status_code=401, detail="Incomplete trusted business identity context")
    return identity


def identity_owner_payload(value: Any) -> dict[str, str]:
    identity = require_complete_platform_identity(value)
    if identity.is_empty:
        return {}
    return {
        "identity_namespace": identity.identity_namespace,
        "tenant_id": identity.tenant_id,
        "subject_type": identity.subject_type,
        "subject_id": identity.subject_id,
    }


def identity_digest(value: Any) -> str:
    owner = identity_owner_payload(value)
    if not owner:
        return ""
    canonical = json.dumps(owner, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def identity_native_user_id(value: Any) -> str:
    """Return an opaque collision-resistant native storage user key."""

    digest = identity_digest(value)
    return f"aeu_{digest}" if digest else ""


def identity_scope_ref(value: Any) -> str:
    digest = identity_digest(value)
    return f"aei_{digest}" if digest else ""


def identity_session_is_legacy(value: Any, session_user_id: str) -> bool:
    """Return whether an authorized canonical row still uses its legacy user key."""

    identity = require_complete_platform_identity(value)
    return bool(
        not identity.is_empty
        and session_user_id
        and session_user_id != identity_native_user_id(identity)
    )


def identity_can_adopt_any_legacy_user(value: Any) -> bool:
    """Whether this verified principal owns the whole legacy Agent runtime."""

    identity = require_complete_platform_identity(value)
    return bool(
        not identity.is_empty
        and identity.identity_namespace == DEFAULT_IAM_IDENTITY_NAMESPACE
        and identity.subject_type == "account"
    )


def identity_can_adopt_legacy_user(value: Any, legacy_user_id: str) -> bool:
    """Whether a verified IAM principal deterministically owns a legacy user."""

    identity = require_complete_platform_identity(value)
    # ``aeu_`` is reserved for identity-aware rows. If a concurrent creator
    # has written the opaque key but has not persisted its binding yet, no
    # other principal may mistake that row for legacy data and claim it.
    if str(legacy_user_id or "").startswith("aeu_"):
        return False
    if identity_can_adopt_any_legacy_user(identity):
        return True
    return bool(
        not identity.is_empty
        and identity.identity_namespace == DEFAULT_IAM_IDENTITY_NAMESPACE
        and identity.subject_type == "user"
        and str(legacy_user_id or "") == identity.subject_id
    )


def _binding_payload(identity: PlatformIdentityContext, native_user_id: str) -> dict[str, Any]:
    return {
        "version": SESSION_IDENTITY_BINDING_VERSION,
        "owner": identity_owner_payload(identity),
        "scope_ref": identity_scope_ref(identity),
        "native_user_id": native_user_id,
    }


async def _read_binding(
    service: BaseSessionService,
    session: Session,
) -> dict[str, Any]:
    state = await service.get_state(
        agent_id=session.agent_id,
        user_id=session.user_id,
        session_id=session.id,
        scope=SESSION_IDENTITY_SCOPE,
    )
    return dict(state.state) if state is not None else {}


async def session_identity_binding_matches(
    *,
    service: BaseSessionService,
    session: Session,
    identity: Any,
) -> bool:
    resolved = require_complete_platform_identity(identity)
    if resolved.is_empty:
        return False
    binding = await _read_binding(service, session)
    return bool(binding and _binding_matches(binding, resolved, session_user_id=session.user_id))


def _binding_matches(
    binding: Mapping[str, Any],
    identity: PlatformIdentityContext,
    *,
    session_user_id: str,
) -> bool:
    return (
        dict(binding.get("owner") or {}) == identity_owner_payload(identity)
        and str(binding.get("scope_ref") or "") == identity_scope_ref(identity)
        and str(binding.get("native_user_id") or "") == session_user_id
    )


def _ownership_conflict() -> HTTPException:
    # Do not reveal which principal owns a guessed session id.
    return HTTPException(status_code=404, detail="Session not found")


async def bind_or_validate_session_identity(
    *,
    service: BaseSessionService,
    session: Session,
    identity: Any,
    requested_user_id: str,
) -> Session:
    """Validate an existing session or durably adopt an authorized legacy row."""

    resolved = require_complete_platform_identity(identity)
    if resolved.is_empty:
        # Legacy/local callers historically resume with SessionId alone. Keep
        # that behavior when no trusted identity exists, while an explicitly
        # supplied UserId must still match. Hosted traffic receives a verified
        # default IAM identity and therefore never relies on this fallback.
        if requested_user_id and session.user_id != requested_user_id:
            raise HTTPException(
                status_code=409,
                detail="Session id belongs to a different agent or user",
            )
        return session

    binding = await _read_binding(service, session)
    if binding:
        if not _binding_matches(binding, resolved, session_user_id=session.user_id):
            raise _ownership_conflict()
        return session

    expected_native_user_id = identity_native_user_id(resolved)
    # A newly-created identity-aware row already carries the opaque user key.
    # Legacy adoption is intentionally limited to the built-in IAM mapping:
    # an account principal owns the historical Agent runtime, while an IAM
    # user may only recover the row whose old UserId equals its verified
    # subject. Customer-defined tenants must use a controlled migration; a
    # caller-supplied UserId can never make the first visitor the owner.
    if session.user_id != expected_native_user_id:
        if not identity_can_adopt_legacy_user(resolved, session.user_id):
            raise _ownership_conflict()

    await service.update_state(
        agent_id=session.agent_id,
        user_id=session.user_id,
        session_id=session.id,
        scope=SESSION_IDENTITY_SCOPE,
        state_delta=_binding_payload(resolved, session.user_id),
    )
    # Re-read after the write.  Backends serialize individual state updates;
    # this also fails closed if a competing claimant replaced the binding.
    persisted = await _read_binding(service, session)
    if not _binding_matches(persisted, resolved, session_user_id=session.user_id):
        raise _ownership_conflict()
    return session


__all__ = [
    "SESSION_IDENTITY_SCOPE",
    "bind_or_validate_session_identity",
    "coerce_platform_identity",
    "identity_digest",
    "identity_can_adopt_any_legacy_user",
    "identity_can_adopt_legacy_user",
    "identity_native_user_id",
    "identity_owner_payload",
    "identity_session_is_legacy",
    "identity_scope_ref",
    "require_complete_platform_identity",
    "session_identity_binding_matches",
]
