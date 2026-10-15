"""Host policy and persisted model-assisted diagnostics; no verifier authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeliveryReviewPolicy(_Contract):
    path_suffixes: tuple[str, ...] = Field(min_length=1, max_length=64)
    max_rounds: int = Field(default=2, strict=True, ge=1, le=4)
    max_read_rounds: int = Field(default=2, strict=True, ge=0, le=4)
    # Directory fallback can prove a content change without identifying paths.
    # The host chooses conservative review of that change rather than assuming
    # it cannot affect a delivery. Read-only runs have no mutation cursor.
    review_unknown_paths: bool = True

    @model_validator(mode="after")
    def valid_suffixes(self):
        if any(not s.startswith(".") or s != s.lower() or "/" in s for s in self.path_suffixes):
            raise ValueError("review suffixes must be lowercase file suffixes")
        return self


class RequirementReview(_Contract):
    requirement: str = Field(min_length=1, max_length=2048, pattern=r"\S")
    implementation_evidence: str = Field(min_length=1, max_length=2048, pattern=r"\S")
    verification_evidence: str = Field(min_length=1, max_length=2048, pattern=r"\S")


class DeliveryFinding(_Contract):
    issue: str = Field(min_length=1, max_length=2048, pattern=r"\S")
    recommended_check: str = Field(min_length=1, max_length=2048, pattern=r"\S")


class DeliveryReviewSuggestion(_Contract):
    requirements: tuple[RequirementReview, ...] = Field(min_length=1, max_length=32)
    findings: tuple[DeliveryFinding, ...] = Field(max_length=16)


class DeliveryReviewState(_Contract):
    status: Literal["pending", "observed", "unavailable"]
    rounds: int = Field(strict=True, ge=1, le=4)
    subject_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_subjects: tuple[str, ...] = Field(min_length=1, max_length=4)
    suggestion: DeliveryReviewSuggestion | None = None
    reason: str | None = None
    # Private review suffix protocol, never appended to the implementing
    # agent's transcript or inherited by its next Session.
    continuation_messages: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=8)
    read_rounds: int = Field(default=0, strict=True, ge=0, le=4)
