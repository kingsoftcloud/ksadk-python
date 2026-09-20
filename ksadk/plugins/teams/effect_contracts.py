"""teams-effects/v1 wire DTOs shared by Host, Server and Web.

Transport parsing never authorizes a caller; consumers verify the original
scope, canonical evidence digest and current execution permission separately.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cloud_contracts import Digest, Identifier, OperationKey, TeamsExecutionRef, WireModel, digest
from .cloud_permits import TeamsExecutionPermit

EFFECTS_PORT_VERSION = "teams-effects/v1"


class EffectRequest(WireModel):
    """Construct from the trusted Host context, never a model's identity claims."""

    ref: TeamsExecutionRef
    context_ref: Identifier
    store_incarnation: Identifier
    journal_incarnation: Identifier
    native_run_id: Identifier
    tool_call_id: Identifier
    effect_index: int = Field(strict=True, ge=0, le=1023)
    tool_name: Identifier
    adapter_version: Identifier
    effect_class: Literal["external_idempotent", "external_reconcilable"]
    payload_digest: Digest

    @model_validator(mode="after")
    def native_identity(self):
        if self.ref.nativeRunId not in {None, self.native_run_id}:
            raise ValueError("effect_native_run_mismatch")
        return self

    @property
    def effect_key(self) -> str:
        return (
            "effect_"
            + digest(
                {
                    "authorityId": self.ref.authorityId,
                    "executionCommandId": self.ref.commandId,
                    "toolCallId": self.tool_call_id,
                    "effectIndex": self.effect_index,
                }
            )[7:]
        )

    def stable(self) -> dict:
        value = self.model_dump(mode="json")
        # Scheduler ownership may change while original execution remains valid.
        value["ref"].pop("schedulerEpoch")
        value["ref"]["nativeRunId"] = self.native_run_id
        return value

    @property
    def request_digest(self) -> str:
        return digest(self.stable())

    @property
    def invocation_key(self) -> str:
        return (
            "invocation_"
            + digest(
                {
                    "authorityId": self.ref.authorityId,
                    "commandId": self.ref.commandId,
                    "toolCallId": self.tool_call_id,
                }
            )[7:]
        )

    @property
    def invocation_digest(self) -> str:
        value = self.stable()
        value.pop("effect_index")
        return digest(value)


def effect_payload_digest(tool_name: str, adapter_version: str, arguments: dict) -> str:
    return digest(
        {"toolName": tool_name, "adapterVersion": adapter_version, "arguments": arguments}
    )


class EffectOutcome(WireModel):
    """Only adapter-proven outcomes; timeouts/ambiguous failure use no outcome.

    Evidence and result are authorized opaque references/digests, never raw
    tool results or credentials. Adapters own redaction of external references.
    A failed outcome requires proof that nothing was applied.
    """

    phase: Literal["completed", "failed"]
    external_ref: Identifier | None = None
    evidence_ref: Identifier
    result_digest: Digest | None = None
    definitively_not_applied: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def proven(self):
        if (self.phase == "failed") != self.definitively_not_applied:
            raise ValueError("effect_outcome_not_proven")
        return self


class EffectPreparedReceipt(WireModel):
    effect_key: Identifier
    payload_digest: Digest
    request_digest: Digest
    revision: int = Field(strict=True, ge=1, le=9007199254740991)
    phase: Literal["prepared", "unknown", "completed", "failed", "resolved"]


class EffectRecord(WireModel):
    request: EffectRequest
    phase: Literal["prepared", "unknown", "completed", "failed"]
    revision: int = Field(strict=True, ge=1, le=9007199254740991)
    evidence_digest: Digest
    outcome: EffectOutcome | None = None

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.phase in {"completed", "failed"}:
            if self.outcome is None or self.outcome.phase != self.phase:
                raise ValueError("effect_outcome_missing")
        elif self.outcome is not None:
            raise ValueError("effect_outcome_unexpected")
        return self


class EffectReportReceipt(WireModel):
    effectKey: Identifier
    journalRevision: int = Field(strict=True, ge=1, le=9007199254740991)
    evidenceDigest: Digest
    authorityRevision: int = Field(strict=True, ge=1, le=9007199254740991)
    phase: Literal["prepared", "unknown", "completed", "failed", "resolved"]


class ExecutionEffectsPage(WireModel):
    contextRef: Identifier
    storeIncarnation: Identifier
    nativeRunId: Identifier
    journalIncarnation: Identifier
    items: list[EffectRecord] = Field(max_length=100)
    nextCursor: str | None = None


class EffectsRequest(BaseModel):
    """Existing GetExecutionEffects HTTP envelope; original Host limits retained."""

    model_config = ConfigDict(extra="forbid")
    contextRef: str = Field(min_length=1)
    expectedIncarnation: str = Field(min_length=1)
    permit: TeamsExecutionPermit
    after: str = Field(default="", max_length=256)
    limit: int = Field(default=100, ge=1, le=100)

    def arguments(self):
        return dict(
            context_ref=self.contextRef,
            expected_incarnation=self.expectedIncarnation,
            permit=self.permit,
        )


class EffectReconcileInput(WireModel):
    expectedRevision: int = Field(strict=True, ge=1, le=9007199254740991)
    expectedEvidenceDigest: Digest
    decision: Literal["confirmed_applied", "confirmed_not_applied", "accept_risk"]
    evidenceRef: Identifier
    reason: str = Field(strict=True, min_length=1, max_length=1000)
    idempotencyKey: OperationKey


class EffectOwnerScope(WireModel):
    authorityId: str = Field(strict=True, min_length=1, max_length=256)
    ownerScopeRef: str = Field(strict=True, min_length=1, max_length=512)
    groupId: str = Field(strict=True, min_length=1, max_length=256)


class EffectResolution(WireModel):
    resolutionId: Identifier
    expectedRevision: int = Field(strict=True, ge=1, le=9007199254740991)
    expectedEvidenceDigest: Digest
    decision: Literal["confirmed_applied", "confirmed_not_applied", "accept_risk"]
    evidenceRef: Identifier
    reason: str = Field(strict=True, min_length=1, max_length=1000)
    kind: Literal["manual_risk_acceptance", "external_evidence"]
    actorSubject: Identifier
    ownerScopeRef: Identifier
    verifiedOutcome: EffectOutcome | None

    @model_validator(mode="after")
    def verified(self):
        if self.decision == "accept_risk":
            if self.kind != "manual_risk_acceptance" or self.verifiedOutcome is not None:
                raise ValueError("effect_resolution_not_verified")
        elif (
            self.kind != "external_evidence"
            or self.verifiedOutcome is None
            or self.verifiedOutcome.phase
            != ("completed" if self.decision == "confirmed_applied" else "failed")
        ):
            raise ValueError("effect_resolution_not_verified")
        return self


class EffectProjection(WireModel):
    effectKey: Identifier
    groupId: Identifier
    teamRunId: Identifier
    toolName: Identifier
    effectClass: Literal["external_idempotent", "external_reconcilable"]
    phase: Literal["prepared", "unknown", "completed", "failed", "resolved"]
    revision: int = Field(strict=True, ge=1, le=9007199254740991)
    evidenceDigest: Digest
    resolution: EffectResolution | None
    resolutionConflict: bool = Field(strict=True)
    outcome: EffectOutcome | None

    @model_validator(mode="after")
    def consistent(self):
        if self.phase == "resolved":
            if self.resolution is None:
                raise ValueError("effect_resolution_missing")
        elif (
            self.resolution is not None
            or self.resolutionConflict
            or (
                self.phase in {"completed", "failed"}
                and (self.outcome is None or self.outcome.phase != self.phase)
            )
            or (self.phase not in {"completed", "failed"} and self.outcome is not None)
        ):
            raise ValueError("effect_projection_inconsistent")
        return self


class EffectProjectionPage(WireModel):
    scope: EffectOwnerScope
    teamRunId: Identifier
    items: list[EffectProjection] = Field(max_length=100)
    nextCursor: int | None = Field(ge=1, le=9007199254740991)

    @model_validator(mode="after")
    def unique(self):
        if len({item.effectKey for item in self.items}) != len(self.items):
            raise ValueError("duplicate_effect_identity")
        return self
