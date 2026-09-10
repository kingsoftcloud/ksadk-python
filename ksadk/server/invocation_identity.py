"""Private trusted-identity carrier for hosted runtime requests."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import Header, HTTPException

from ksadk.runtime_context import TRUSTED_IDENTITY_METADATA_KEY, PlatformIdentityContext


def _header_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def trusted_invocation_identity_from_headers(
    headers: Mapping[str, Any],
) -> PlatformIdentityContext:
    """Resolve the private carrier from an ASGI/HTTP header mapping."""

    return _resolve_trusted_invocation_identity_values(
        identity_namespace=headers.get("x-agentengine-identity-namespace"),
        tenant_id=headers.get("x-agentengine-business-tenant-id"),
        subject_type=headers.get("x-agentengine-subject-type"),
        subject_id=headers.get("x-agentengine-subject-id"),
    )


def _resolve_trusted_invocation_identity_values(
    *,
    identity_namespace: Any,
    tenant_id: Any,
    subject_type: Any,
    subject_id: Any,
) -> PlatformIdentityContext:
    values = {
        "identity_namespace": _header_value(identity_namespace),
        "tenant_id": _header_value(tenant_id),
        "subject_type": _header_value(subject_type),
        "subject_id": _header_value(subject_id),
    }
    present = {field for field, value in values.items() if value}
    if not present:
        return PlatformIdentityContext()
    if present != set(values):
        raise HTTPException(status_code=401, detail="Incomplete trusted business identity context")
    limits = {"identity_namespace": 128, "tenant_id": 128, "subject_type": 32, "subject_id": 128}
    for field, value in values.items():
        if len(value) > limits[field] or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid trusted business identity field: {field}",
            )
    return PlatformIdentityContext.from_payload(values)


def trusted_invocation_identity_headers(identity: Any) -> dict[str, str]:
    """Build the private header carrier for an in-process runtime hop."""

    resolved = coerce_trusted_invocation_identity(identity)
    if resolved.is_empty:
        return {}
    return {
        "X-AgentEngine-Identity-Namespace": resolved.identity_namespace,
        "X-AgentEngine-Business-Tenant-Id": resolved.tenant_id,
        "X-AgentEngine-Subject-Type": resolved.subject_type,
        "X-AgentEngine-Subject-Id": resolved.subject_id,
    }


def resolve_trusted_invocation_identity(
    x_agentengine_identity_namespace: str | None = Header(
        None, alias="X-AgentEngine-Identity-Namespace"
    ),
    x_agentengine_business_tenant_id: str | None = Header(
        None, alias="X-AgentEngine-Business-Tenant-Id"
    ),
    x_agentengine_subject_type: str | None = Header(None, alias="X-AgentEngine-Subject-Type"),
    x_agentengine_subject_id: str | None = Header(None, alias="X-AgentEngine-Subject-Id"),
) -> PlatformIdentityContext:
    """Resolve the all-or-none identity rebuilt by the private gateway hop."""

    return _resolve_trusted_invocation_identity_values(
        identity_namespace=x_agentengine_identity_namespace,
        tenant_id=x_agentengine_business_tenant_id,
        subject_type=x_agentengine_subject_type,
        subject_id=x_agentengine_subject_id,
    )


def coerce_trusted_invocation_identity(value: Any) -> PlatformIdentityContext:
    """Keep direct unit calls compatible with FastAPI's dependency default."""

    return value if isinstance(value, PlatformIdentityContext) else PlatformIdentityContext()


def inject_trusted_invocation_identity(
    metadata: Mapping[str, Any] | None,
    identity: Any,
) -> dict[str, Any]:
    """Overwrite the private key; caller metadata can never create this context."""

    result = dict(metadata or {})
    result.pop(TRUSTED_IDENTITY_METADATA_KEY, None)
    resolved = coerce_trusted_invocation_identity(identity)
    if not resolved.is_empty:
        result[TRUSTED_IDENTITY_METADATA_KEY] = resolved.to_payload()
    return result


__all__ = [
    "TRUSTED_IDENTITY_METADATA_KEY",
    "coerce_trusted_invocation_identity",
    "inject_trusted_invocation_identity",
    "resolve_trusted_invocation_identity",
    "trusted_invocation_identity_from_headers",
    "trusted_invocation_identity_headers",
]
