"""Invocation state contracts; no transport or persistence side effects."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ksadk.a2a.invocation_types import (
    A2AContinuationRequest,
    A2AInvocationHandle,
    A2AInvocationOutputRef,
    A2AInvocationRequest,
    A2AInvocationSnapshot,
)
from ksadk.a2a.presentation import ExecutionScopeDescriptor
from ksadk.events.adapters.a2a import A2AAdapterContext, A2AEventAdapter
from ksadk.events.canonical import InteractionRequested, RuntimeEvent


class A2AInvocationEventStore(Protocol):
    async def persist_one(
        self, session_id: str, event: RuntimeEvent
    ) -> tuple[RuntimeEvent, bool]: ...
    async def list(self, session_id: str, *, run_id: str | None = None) -> list[RuntimeEvent]: ...


@dataclass(frozen=True)
class A2AInvocationTimeouts:
    prepare_seconds: float = 15.0
    connect_seconds: float = 15.0
    first_event_seconds: float = 30.0
    idle_seconds: float = 30.0

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("A2A_TIMEOUT_INVALID")


@dataclass
class _State:
    request: A2AInvocationRequest
    handle: A2AInvocationHandle
    descriptor: ExecutionScopeDescriptor
    adapter: A2AEventAdapter = field(default_factory=A2AEventAdapter)
    context: A2AAdapterContext | None = None
    snapshot: A2AInvocationSnapshot | None = None
    send_entered: bool = False
    active: bool = False
    tokens: dict[str, str] = field(default_factory=dict)
    refs: dict[tuple[str, str | None], A2AInvocationOutputRef] = field(default_factory=dict)
    continuation_id: str | None = None
    pending_continuation: A2AContinuationRequest | None = None
    pending_input: InteractionRequested | None = None
    source: str = "local"
    cursor: int = 0
    last_event_id: str | None = None
    pending: list[RuntimeEvent] = field(default_factory=list)
    cancel_checked_for: tuple | None = None
    pending_native: asyncio.Task | None = None


@dataclass
class _CancelDelivery:
    events: list[RuntimeEvent]
    terminal: bool


class _ServiceCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    descriptor: ExecutionScopeDescriptor
    adapter: dict[str, JsonValue] | None = None
    snapshot: A2AInvocationSnapshot | None = None
    tokens: dict[str, str] = Field(default_factory=dict)
    refs: list[A2AInvocationOutputRef] = Field(default_factory=list)
    continuation_id: str | None = None
    pending_continuation: A2AContinuationRequest | None = None
    pending_input: InteractionRequested | None = None
    source: str = "local"
    cursor: int = Field(default=0, ge=0)
    last_event_id: str | None = None
    pending: list[dict[str, JsonValue]] = Field(default_factory=list)
