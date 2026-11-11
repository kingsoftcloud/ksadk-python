"""Read-only host observations of already-issued command receipts."""

from typing import Literal, Protocol

from pydantic import Field

from .state import BackgroundCommandRef, Contract, RunScope


class BackgroundCommandRequest(Contract):
    scope: RunScope
    run_id: str
    session_id: str
    refs: tuple[BackgroundCommandRef, ...]


class BackgroundCommandObservation(Contract):
    ref: BackgroundCommandRef
    status: Literal[
        "completed", "failed", "cancelled", "timed_out", "output_limit", "unknown", "unavailable"
    ]
    exit_code: int | None = Field(default=None, strict=True)
    stdout: str = Field(default="", max_length=8192)
    stderr: str = Field(default="", max_length=8192)
    stdout_cursor: int = Field(default=0, strict=True, ge=0)
    stderr_cursor: int = Field(default=0, strict=True, ge=0)
    output_may_be_truncated: bool = Field(default=False, strict=True)
    # Controlled evidence for a reconnect gap: the outcome is unknown with
    # incomplete evidence even if an exit code was observed. Backward-compatible
    # defaults so older observers/readers continue to work.
    evidence_incomplete: bool = Field(default=False, strict=True)
    observed_exit_code: int | None = Field(default=None, strict=True)


class BackgroundCommandPort(Protocol):
    def observe(
        self, request: BackgroundCommandRequest,
    ) -> tuple[BackgroundCommandObservation, ...]:
        """Read only; never launch, cancel, grant authority or start a model turn."""
        ...
