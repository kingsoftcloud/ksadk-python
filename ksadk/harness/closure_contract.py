"""Stable contracts used to close and verify Harness runtime capabilities.

The models intentionally carry metadata only.  Raw provider errors, tool results,
credentials, and other sensitive payloads do not belong in closure reports.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunTerminalStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"
    INTERRUPTED = "interrupted"

    @classmethod
    def normalize(cls, value: str | "RunTerminalStatus") -> "RunTerminalStatus":
        if isinstance(value, cls):
            return value
        if value == "canceled":
            value = cls.CANCELLED.value
        return cls(value)


class ReadinessStatus(str, Enum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"
    NOT_CONFIGURED = "not_configured"


class FailureCategory(BaseModel):
    """Policy-safe failure identity; human error text remains in a redacted cause."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    domain: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    retryable: bool = False


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    check_id: str = Field(alias="id", min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=64)
    status: ReadinessStatus
    required: bool = True
    reason_code: str = Field(min_length=1, max_length=128)


class HarnessClosureReport(BaseModel):
    """Versioned machine-readable closure result shared by SDK and CI.

    ``extra='ignore'`` is the additive compatibility rule: readers of schema v1
    accept metadata appended by a newer v1 writer without treating it as proof.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schemaVersion", ge=1)
    target: str = Field(min_length=1, max_length=256)
    status: ReadinessStatus
    checks: tuple[ReadinessCheck, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def populate_counts(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("counts") is not None:
            return value
        checks = value.get("checks") or ()
        counts = {status.value: 0 for status in ReadinessStatus}
        for check in checks:
            raw = check.status if isinstance(check, ReadinessCheck) else check.get("status")
            counts[ReadinessStatus(raw).value] += 1
        return {**value, "counts": counts}


__all__ = [
    "FailureCategory",
    "HarnessClosureReport",
    "ReadinessCheck",
    "ReadinessStatus",
    "RunTerminalStatus",
]
