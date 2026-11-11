"""Non-serialized native delivery facts carried inside the existing event pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import PrivateAttr

from ksadk.events.canonical import InteractionRequested, RunProgress


@dataclass(frozen=True)
class NativeSubmissionOutcome:
    interaction_id: str
    attempt_key: str
    status: Literal["consumed", "rejected", "unknown"]


class NativeInteractionRequested(InteractionRequested):
    _native_target: dict[str, Any] = PrivateAttr(default_factory=dict)
    _presentation: dict[str, Any] | None = PrivateAttr(default=None)


class NativeSubmissionProgress(RunProgress):
    _outcomes: tuple[NativeSubmissionOutcome, ...] = PrivateAttr(default=())


__all__ = ["NativeInteractionRequested", "NativeSubmissionOutcome", "NativeSubmissionProgress"]
