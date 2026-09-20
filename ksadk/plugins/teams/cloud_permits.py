"""Short-lived, audience-separated permits for Teams trusted host traffic.

Signatures establish the transport caller only. Callers must additionally
compare immutable claims with the original persisted operation and enforce the
current grant/attempt. This module never accepts public-key material from a
permit and never generates a production signing key.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, model_validator

from .cloud_contracts import (
    CommandId,
    Digest,
    ExecutionTarget,
    Identifier,
    Revision,
    Timestamp,
    WireModel,
    canonical_bytes,
)

EXECUTE_OPERATIONS = frozenset(
    {
        "ensure_session",
        "prepare",
        "enqueue",
        "renew_grant",
        "set_grant",
        "set_admission",
        "invoke",
        "respond_interaction",
    }
)
RECOVERY_OPERATIONS = frozenset(
    {
        "lookup",
        "get_result",
        "observe",
        "get_grant",
        "revoke",
        "cancel",
    }
)
CALLBACK_EXECUTE_OPERATIONS = frozenset(
    {
        "policy",
        "invoke",
        "effects",
        "read_material",
        "create_artifact",
        "upload_artifact",
        "finalize_artifact",
    }
)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone-aware clock required")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PermitError(ValueError):
    """Redacted transport authorization error safe to map to an HTTP failure."""


class PermitBase(WireModel):
    schemaVersion: Literal[1] = 1
    issuer: Identifier
    kid: Identifier
    authorityId: Identifier
    issuedAt: Timestamp
    expiresAt: Timestamp
    nonce: Identifier
    signature: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_lifetime(self) -> PermitBase:
        ttl = (_time(self.expiresAt) - _time(self.issuedAt)).total_seconds()
        if not 0 < ttl <= 60:
            raise ValueError("Teams permit lifetime must be at most 60 seconds")
        return self

    def signing_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json", exclude={"signature"}))


class BindingProbePermit(PermitBase):
    audience: Literal["agentengine-teams-probe"] = "agentengine-teams-probe"
    probeId: CommandId
    bindingRef: Identifier
    target: ExecutionTarget
    expectedDigests: dict[Literal["bundle", "contract", "capabilities"], Digest] = Field(
        min_length=3, max_length=3
    )
    allowedOperations: list[Literal["describe"]] = Field(min_length=1, max_length=1)


class TeamsExecutionPermit(PermitBase):
    audience: Literal["agentengine-teams-runtime"] = "agentengine-teams-runtime"
    permitKind: Literal["execute", "recovery"]
    subjectRef: Identifier
    agentInstanceId: Identifier
    sessionId: Identifier
    commandId: CommandId
    allowedOperations: list[str] = Field(min_length=1, max_length=16)
    payloadDigest: Digest
    policyDigest: Digest
    leaderEpoch: Revision
    dispatchEpoch: Revision
    attemptEpoch: Revision
    grantRevision: Revision

    @model_validator(mode="after")
    def validate_operations(self) -> TeamsExecutionPermit:
        allowed = EXECUTE_OPERATIONS if self.permitKind == "execute" else RECOVERY_OPERATIONS
        if not set(self.allowedOperations) <= allowed:
            raise ValueError("operation is not allowed by this permit kind")
        if len(self.allowedOperations) != len(set(self.allowedOperations)):
            raise ValueError("duplicate permit operation")
        return self


class TeamsCallbackPermit(PermitBase):
    audience: Literal["agentengine-teams-callback"] = "agentengine-teams-callback"
    permitKind: Literal["execute", "recovery"]
    groupId: Identifier
    teamRunId: Identifier
    memberId: Identifier
    commandId: CommandId
    sessionId: Identifier
    agentInstanceId: Identifier
    bundleDigest: Digest
    policyDigest: Digest
    attemptEpoch: Revision
    leaderEpoch: Revision
    grantRevision: Revision
    allowedOperations: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_operations(self) -> TeamsCallbackPermit:
        if (_time(self.expiresAt) - _time(self.issuedAt)).total_seconds() > 30:
            raise ValueError("callback permit lifetime must be at most 30 seconds")
        allowed = (
            CALLBACK_EXECUTE_OPERATIONS
            if self.permitKind == "execute"
            else {"append_effect_evidence"}
        )
        if not set(self.allowedOperations) <= allowed:
            raise ValueError("operation is not allowed by callback permit kind")
        if len(self.allowedOperations) != len(set(self.allowedOperations)):
            raise ValueError("duplicate permit operation")
        return self


class Signer(Protocol):
    key_id: str

    def sign(self, message: bytes) -> str: ...


def sign_permit(permit: PermitBase, signer: Signer) -> PermitBase:
    if permit.kid != signer.key_id:
        raise PermitError("signing key does not match permit")
    return permit.model_copy(update={"signature": signer.sign(permit.signing_bytes())})


class TeamsPermitVerifier:
    """Trusted keys are supplied by the authenticated JWKS/configuration path."""

    def __init__(self, keys: Mapping[str, Ed25519PublicKey], *, issuer: str):
        self._keys = dict(keys)
        self.issuer = issuer

    def verify(
        self,
        value: Any,
        model: type[BindingProbePermit] | type[TeamsExecutionPermit] | type[TeamsCallbackPermit],
        *,
        operation: str,
        expected: Mapping[str, Any],
        now: datetime,
        grant_expires_at: datetime | None = None,
    ) -> PermitBase:
        try:
            permit = model.model_validate(value)
        except ValueError as exc:
            raise PermitError("invalid Teams permit") from exc
        key = self._keys.get(permit.kid)
        if key is None or permit.issuer != self.issuer:
            raise PermitError("untrusted Teams permit issuer or key")
        try:
            signature = base64.b64decode(
                permit.signature + "=" * (-len(permit.signature) % 4), altchars=b"-_", validate=True
            )
            key.verify(signature, permit.signing_bytes())
        except (ValueError, InvalidSignature) as exc:
            raise PermitError("invalid Teams permit signature") from exc
        if now.tzinfo is None:
            raise ValueError("timezone-aware verification clock required")
        if not _time(permit.issuedAt) <= now < _time(permit.expiresAt):
            raise PermitError("Teams permit is not currently valid")
        if operation not in permit.allowedOperations:
            raise PermitError("Teams operation not permitted")
        claims = permit.model_dump(mode="json")
        required = {"authorityId"}
        if isinstance(permit, BindingProbePermit):
            required |= {"probeId", "bindingRef", "target", "expectedDigests"}
        else:
            required |= {
                "agentInstanceId",
                "sessionId",
                "commandId",
                "policyDigest",
                "attemptEpoch",
                "grantRevision",
            }
            if isinstance(permit, TeamsExecutionPermit):
                required |= {"subjectRef", "payloadDigest"}
            else:
                required |= {"groupId", "teamRunId", "memberId", "bundleDigest"}
        if not required <= expected.keys() or any(
            key not in claims or claims[key] != value for key, value in expected.items()
        ):
            raise PermitError("Teams permit scope does not match persisted operation")
        if getattr(permit, "permitKind", None) == "execute":
            if grant_expires_at is None or grant_expires_at.tzinfo is None:
                raise PermitError("active execution grant is required")
            if now >= grant_expires_at or _time(permit.expiresAt) > grant_expires_at:
                raise PermitError("Teams permit exceeds execution grant lifetime")
        return permit


PERMIT_MODELS = {
    "binding-probe-permit": BindingProbePermit,
    "execution-permit": TeamsExecutionPermit,
    "callback-permit": TeamsCallbackPermit,
}
