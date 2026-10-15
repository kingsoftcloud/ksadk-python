"""Resolve a Native RunScope from Studio's authenticated activation claims."""

from __future__ import annotations

from ksadk.harness.native.state import RunScope
from ksadk.plugins.providers.harness import HarnessEngineActivationContext


def native_scope_from_activation(context: HarnessEngineActivationContext) -> RunScope:
    """Bind one activated Build to its host-owned tenant, workspace and session.

    These claims originate in Studio after it checks the session owner. A
    builder must not substitute a process-wide or test-only scope.
    """
    claims = dict(context.activation_claims)
    if len(claims) != len(context.activation_claims):
        raise ValueError("native_activation_claims_duplicate")
    if (
        not claims.get("session_id")
        or claims["session_id"] != context.activation_key
        or claims.get("agent_id") != context.bundle.manifest.agent_id
    ):
        raise ValueError("native_activation_identity_mismatch")
    try:
        scope = RunScope(
            tenant_id=claims["tenant_id"],
            workspace_id=claims["workspace_id"],
            user_id=claims["user_id"],
            agent_id=claims["agent_id"],
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("native_activation_scope_unavailable") from error
    if any(not value.strip() for value in scope.model_dump().values()):
        raise ValueError("native_activation_scope_unavailable")
    return scope
