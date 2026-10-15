"""Safe host failure provenance, separate from authoritative Native run facts.

This carries no retry grant, exception text, arbitrary class name or traceback.
A driver which cannot commit must not fabricate a Native terminal to explain
its own exit. Studio may persist this diagnostic alongside its host failure.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from pydantic import Field, model_validator

from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.native.state import Contract, RunPhase, RunSnapshot
from ksadk.harness.stores.native_contract import (
    EffectAlreadyClaimed,
    LeaseBusy,
    PendingCommandConflict,
    ScopeMismatch,
    StoreConflict,
)

FailureCode = Literal[
    "store_conflict", "scope_mismatch", "lease_busy", "effect_already_claimed",
    "pending_command_conflict", "sqlite_error", "timeout_error", "connection_error",
    "runtime_error", "value_error", "type_error", "key_error", "driver_canceled", "other",
]
EffectKind = Literal["workspace", "model", "tool", "verify"]

_KNOWN_TYPES: dict[type[BaseException], FailureCode] = {
    StoreConflict: "store_conflict",
    ScopeMismatch: "scope_mismatch",
    LeaseBusy: "lease_busy",
    EffectAlreadyClaimed: "effect_already_claimed",
    PendingCommandConflict: "pending_command_conflict",
    sqlite3.Error: "sqlite_error",
    sqlite3.DatabaseError: "sqlite_error",
    sqlite3.OperationalError: "sqlite_error",
    sqlite3.IntegrityError: "sqlite_error",
    TimeoutError: "timeout_error",
    ConnectionError: "connection_error",
    RuntimeError: "runtime_error",
    ValueError: "value_error",
    TypeError: "type_error",
    KeyError: "key_error",
    asyncio.CancelledError: "driver_canceled",
}


class NativeDriverFailureDiagnostic(Contract):
    version: Literal["native-driver-failure/v1"] = "native-driver-failure/v1"
    reason_code: FailureCode
    cause_depth: int = Field(ge=0, le=7, strict=True)
    phase: RunPhase | Literal["unavailable"] = "unavailable"
    snapshot_revision: int | None = Field(default=None, ge=0, strict=True)
    snapshot_observed: bool = Field(default=False, strict=True)
    effect_kind: EffectKind | None = None
    effect_observed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def observed_facts(self):
        if self.snapshot_observed != (self.phase != "unavailable"):
            raise ValueError("driver phase requires an observed snapshot")
        if self.snapshot_observed != (self.snapshot_revision is not None):
            raise ValueError("driver revision requires an observed snapshot")
        if self.effect_observed != (self.effect_kind is not None):
            raise ValueError("driver effect requires an observed intent")
        if self.effect_observed and not self.snapshot_observed:
            raise ValueError("driver effect requires an observed snapshot")
        return self


class NativeDriverFailure(ExecutionEngineError):
    """A safe host error; its diagnostic does not change the Native ledger."""

    def __init__(self, diagnostic: NativeDriverFailureDiagnostic) -> None:
        super().__init__(
            "native driver stopped unexpectedly; committed state is preserved"
        )
        self.diagnostic = diagnostic


def driver_failure(
    error: BaseException,
    *,
    snapshot: RunSnapshot | None = None,
    effect_kind: EffectKind | None = None,
) -> NativeDriverFailure:
    """Inspect only a bounded explicit cause chain and exact known types.

    Implicit context can be unrelated to this operation. Unknown subclasses
    may put secrets in their names; neither names nor messages are projected.
    The deepest recognized explicit cause is provenance, never retry policy.
    """
    seen: set[int] = set()
    current = error
    reason: FailureCode = "other"
    chosen_depth = 0
    for depth in range(8):
        if id(current) in seen:
            break
        seen.add(id(current))
        code = _KNOWN_TYPES.get(type(current))
        if code is not None:
            reason, chosen_depth = code, depth
        cause = current.__cause__
        if not isinstance(cause, BaseException):
            break
        current = cause
    return NativeDriverFailure(
        NativeDriverFailureDiagnostic(
            reason_code=reason,
            cause_depth=chosen_depth,
            phase=snapshot.phase if snapshot is not None else "unavailable",
            snapshot_revision=snapshot.revision if snapshot is not None else None,
            snapshot_observed=snapshot is not None,
            effect_kind=effect_kind if snapshot is not None else None,
            effect_observed=snapshot is not None and effect_kind is not None,
        )
    )
