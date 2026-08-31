"""Frozen ContextContributor/v1 wire contract and source projections.

The wire contract is deliberately read-only.  A plugin receives an already
authenticated scope and a bounded request, then returns provenance-bearing
fragments.  This module validates that exchange before projecting it into the
existing Context Engine dataclasses; it does not grant filesystem access,
write memory, mutate prompts, or append SessionEvents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast

from pydantic import AwareDatetime, Field, WithJsonSchema, field_validator, model_validator

from ksadk.context_engine.contributors import (
    ContextContributionRequest,
    ContributorCapabilities,
)
from ksadk.context_engine.models import ContextItem, ContextKind, ContextTrustLevel
from ksadk.plugins.contracts import PluginContractModel

ContextClassification = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[a-z][a-z0-9._-]*$",
            "not": {"enum": ["credential", "secret"]},
        }
    ),
]
ContextSourceReference = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 4,
            "maxLength": 2048,
            "pattern": "^[a-z][a-z0-9+.-]*://\\S+$",
            "not": {"pattern": "^(?:credential|env|secret|vault)://"},
        }
    ),
]
ContextExpiry = Annotated[
    AwareDatetime,
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
        }
    ),
]
ContextContributorCacheability = Literal["stable", "turn", "none"]
ContextContributorFailureMode = Literal["skip", "warn", "fail"]

_CLASSIFICATION = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CONTRIBUTOR_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_POLICY_REF = re.compile(r"^policy://[^\s@]+@[^\s@]+$")
_SOURCE_REF = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_CLASSIFICATIONS = {"credential", "secret"}
_FORBIDDEN_SOURCE_SCHEMES = {"credential", "env", "secret", "vault"}
_SENSITIVE_KEY = re.compile(r"(?:secret|password|token|api[_-]?key)", re.IGNORECASE)
_SECRET_REF_PREFIXES = ("secret://", "env://", "credential://", "vault://")
_TRUST_RANK: dict[ContextTrustLevel, int] = {
    "untrusted": 0,
    "user": 1,
    "resource": 2,
    "developer": 3,
    "platform": 4,
}


def _validate_classification(value: str) -> str:
    if not _CLASSIFICATION.fullmatch(value) or value in _FORBIDDEN_CLASSIFICATIONS:
        raise ValueError("classification must be a non-secret qualified name")
    return value


def _validate_secret_references(value: Any, *, path: str) -> None:
    """Permit secret references in metadata, but never clear secret values."""

    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _SENSITIVE_KEY.search(key_text) and child is not None:
                if not isinstance(child, str) or not child.startswith(
                    _SECRET_REF_PREFIXES
                ):
                    raise ValueError(f"{child_path} must contain a secret reference")
            _validate_secret_references(child, path=child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_secret_references(child, path=f"{path}[{index}]")


class AuthenticatedContextScope(PluginContractModel):
    """Identity established by the Host before a Contributor is invoked."""

    authenticated: Literal[True]
    actor_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)


class ContextContributorCapabilities(PluginContractModel):
    """Maximum authority and resource envelope granted to one Contributor."""

    contributor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9._-]*$",
    )
    trust_level: ContextTrustLevel
    max_tokens: int = Field(ge=0)
    timeout_ms: int = Field(ge=1)
    cacheability: ContextContributorCacheability
    failure_mode: ContextContributorFailureMode

    @field_validator("contributor_id")
    @classmethod
    def validate_contributor_id(cls, value: str) -> str:
        if not _CONTRIBUTOR_ID.fullmatch(value):
            raise ValueError("contributorId must be a lowercase qualified name")
        return value

    def to_context_engine(self) -> ContributorCapabilities:
        """Project the frozen wire shape into the installed Context Engine type."""

        return ContributorCapabilities(
            contributor_id=self.contributor_id,
            trust_level=self.trust_level,
            max_tokens=self.max_tokens,
            timeout_ms=self.timeout_ms,
            cacheability=self.cacheability,
            failure_mode=self.failure_mode,
        )


class ContextContributorRequest(PluginContractModel):
    """One authenticated, policy-bound and token-bounded read request."""

    request_format: Literal["ksadk.context-request/v1"]
    scope: AuthenticatedContextScope
    invocation_id: str = Field(min_length=1, max_length=256)
    user_input: str = Field(max_length=131_072)
    workspace_root: str = Field(max_length=4096)
    policy_ref: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^policy://[^\s@]+@[^\s@]+$",
    )
    remaining_budget: int = Field(ge=0)
    allowed_classifications: tuple[ContextClassification, ...] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    metadata: dict[str, Any]

    @field_validator("policy_ref")
    @classmethod
    def validate_policy_ref(cls, value: str) -> str:
        if not _POLICY_REF.fullmatch(value):
            raise ValueError("policyRef must be a pinned policy:// reference")
        return value

    @field_validator("allowed_classifications")
    @classmethod
    def validate_allowed_classifications(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = tuple(_validate_classification(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowedClassifications must be unique")
        return tuple(sorted(normalized))

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_secret_references(value, path="request.metadata")
        return value

    def to_context_engine(self) -> ContextContributionRequest:
        """Project scope and policy facts without losing their wire identity."""

        return ContextContributionRequest(
            user_input=self.user_input,
            session_id=self.scope.session_id,
            invocation_id=self.invocation_id,
            workspace_root=self.workspace_root,
            user_id=self.scope.actor_id,
            agent_id=self.scope.agent_id,
            metadata={
                **self.metadata,
                "authenticated_scope": self.scope.model_dump(
                    by_alias=True, mode="json"
                ),
                "turn_id": self.scope.turn_id,
                "policy_ref": self.policy_ref,
                "remaining_budget": self.remaining_budget,
                "allowed_classifications": list(self.allowed_classifications),
            },
        )


class ContextFragment(PluginContractModel):
    """One read-only, attributed Context candidate returned by a plugin."""

    fragment_format: Literal["ksadk.context-fragment/v1"]
    item_id: str = Field(min_length=1, max_length=256)
    kind: ContextKind
    content: Any
    source_refs: tuple[ContextSourceReference, ...] = Field(
        min_length=1,
        json_schema_extra={"uniqueItems": True},
    )
    classification: ContextClassification
    expires_at: ContextExpiry | None
    token_estimate: int = Field(ge=0)
    trust_level: ContextTrustLevel
    priority: int
    required: Literal[False]
    droppable: bool
    truncatable: bool
    stable: bool
    group_id: str | None = Field(max_length=256)
    seq_start: int | None = Field(ge=0)
    seq_end: int | None = Field(ge=0)
    score: float | None
    content_hash: Annotated[
        str,
        Field(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71),
    ] | None
    metadata: dict[str, Any]

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: Any) -> Any:
        # Structured context is subject to the same secret-reference rule as
        # metadata.  A Contributor cannot bypass admission by moving a clear
        # credential from ``metadata`` into a JSON-shaped ``content`` value.
        _validate_secret_references(value, path="fragment.content")
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("sourceRefs must be unique")
        for source_ref in value:
            if not _SOURCE_REF.fullmatch(source_ref):
                raise ValueError("sourceRefs must be absolute URI-like references")
            scheme, location = source_ref.split("://", 1)
            if scheme in _FORBIDDEN_SOURCE_SCHEMES:
                raise ValueError("sourceRefs cannot point at secret material")
            authority = location.split("/", 1)[0]
            if "@" in authority:
                raise ValueError("sourceRefs cannot contain embedded credentials")
        return tuple(sorted(value))

    @field_validator("classification")
    @classmethod
    def validate_classification(cls, value: str) -> str:
        return _validate_classification(value)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expiresAt must include a timezone")
        return value

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("contentHash must be a lowercase sha256: digest")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_secret_references(value, path="fragment.metadata")
        return value

    @model_validator(mode="after")
    def validate_sequence_range(self) -> "ContextFragment":
        if (self.seq_start is None) != (self.seq_end is None):
            raise ValueError("seqStart and seqEnd must be supplied together")
        if (
            self.seq_start is not None
            and self.seq_end is not None
            and self.seq_end < self.seq_start
        ):
            raise ValueError("seqEnd cannot precede seqStart")
        return self

    def to_context_engine(self) -> ContextItem:
        """Project one already-admitted fragment into a Context Engine item."""

        expires_at = (
            self.model_dump(by_alias=True, mode="json")["expiresAt"]
            if self.expires_at is not None
            else None
        )
        return ContextItem(
            item_id=self.item_id,
            kind=self.kind,
            content=self.content,
            source=self.source_refs[0],
            trust_level=self.trust_level,
            priority=self.priority,
            estimated_tokens=self.token_estimate,
            required=False,
            droppable=self.droppable,
            truncatable=self.truncatable,
            stable=self.stable,
            group_id=self.group_id,
            seq_start=self.seq_start,
            seq_end=self.seq_end,
            score=self.score,
            content_hash=self.content_hash,
            provenance={
                "source_refs": list(self.source_refs),
                "classification": self.classification,
                "expires_at": expires_at,
            },
            metadata=dict(self.metadata),
        )


class ContextContributorResponse(PluginContractModel):
    """Ordered fragments returned for one request."""

    response_format: Literal["ksadk.context-response/v1"]
    fragments: tuple[ContextFragment, ...]

    @model_validator(mode="after")
    def validate_unique_item_ids(self) -> "ContextContributorResponse":
        item_ids = [fragment.item_id for fragment in self.fragments]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("ContextFragment item identity must be unique")
        return self


@dataclass(frozen=True)
class ProjectedContextContribution:
    """Typed bridge into the pre-existing Context Engine source contracts."""

    capabilities: ContributorCapabilities
    request: ContextContributionRequest
    items: tuple[ContextItem, ...]


class ContextContributorExchange(PluginContractModel):
    """Complete golden exchange used by hosts and cross-language conformance."""

    contract_format: Literal["ksadk.context-contributor/v1"]
    capabilities: ContextContributorCapabilities
    request: ContextContributorRequest
    response: ContextContributorResponse

    @model_validator(mode="after")
    def validate_admission_bounds(self) -> "ContextContributorExchange":
        allowed = set(self.request.allowed_classifications)
        maximum_trust = _TRUST_RANK[self.capabilities.trust_level]
        token_ceiling = min(
            self.capabilities.max_tokens,
            self.request.remaining_budget,
        )
        used_tokens = 0
        for fragment in self.response.fragments:
            if fragment.classification not in allowed:
                raise ValueError("ContextFragment classification is not allowed")
            if _TRUST_RANK[fragment.trust_level] > maximum_trust:
                raise ValueError("ContextFragment cannot elevate contributor trust")
            used_tokens += fragment.token_estimate
            if used_tokens > token_ceiling:
                raise ValueError(
                    "ContextFragment token estimate exceeds the effective remaining budget"
                )
        return self

    def project(
        self,
        *,
        now: datetime | None = None,
    ) -> ProjectedContextContribution:
        """Validate expiry and project the admitted response into engine types."""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("projection time must include a timezone")
        for fragment in self.response.fragments:
            if fragment.expires_at is not None and fragment.expires_at <= current:
                raise ValueError(f"ContextFragment {fragment.item_id} has expired")
        return ProjectedContextContribution(
            capabilities=self.capabilities.to_context_engine(),
            request=self.request.to_context_engine(),
            items=tuple(fragment.to_context_engine() for fragment in self.response.fragments),
        )


def context_contributor_json_schema() -> dict[str, Any]:
    """Return the canonical Draft 2020-12 source projection for export gates."""

    schema = ContextContributorExchange.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = (
        "https://ksadk.local/contracts/plugin/v1/context-contributor.schema.json"
    )
    schema["title"] = "ContextContributor/v1 exchange"
    return cast(dict[str, Any], schema)


__all__ = [
    "AuthenticatedContextScope",
    "ContextClassification",
    "ContextContributorCacheability",
    "ContextContributorCapabilities",
    "ContextContributorExchange",
    "ContextContributorFailureMode",
    "ContextContributorRequest",
    "ContextContributorResponse",
    "ContextFragment",
    "ContextSourceReference",
    "ProjectedContextContribution",
    "context_contributor_json_schema",
]
