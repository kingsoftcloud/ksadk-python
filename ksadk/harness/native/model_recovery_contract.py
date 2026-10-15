"""Pure recovery clock and unknown-usage data; no clocks, stores or exchange."""

from __future__ import annotations

from pydantic import Field

from .state import Contract


class RunClockQuote(Contract):
    source_snapshot_digest: str
    measured_elapsed_seconds: float = Field(strict=True, ge=0, allow_inf_nan=False)
    monotonic_seconds: float = Field(strict=True, ge=0, allow_inf_nan=False)
    wall_time: float = Field(strict=True, ge=0, allow_inf_nan=False)
    clock_domain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def unavailable_uncertain_usage(operation_key: str) -> tuple[str, dict[str, str]]:
    return unavailable_model_usage(
        operation_key, f"{operation_key}:unsealed", "model_effect_uncertain"
    )


def unavailable_model_usage(
    request_id: str, attempt_id: str, source: str
) -> tuple[str, dict[str, str]]:
    return (
        "usage.unavailable",
        {
            "semantics": "delta",
            "request_id": request_id,
            "attempt_id": attempt_id,
            "source": source,
        },
    )
