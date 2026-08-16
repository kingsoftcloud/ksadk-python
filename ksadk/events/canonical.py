"""Canonical identity-aware RuntimeEvent schema (schema version 2)."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union, cast, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from ksadk.events.content import ContentSnapshot, ContentUpdate

Framework = Literal["adk", "langgraph", "codex", "a2a", "ksadk"]
SourceProtocol = Literal["a2ui"]
ItemKind = Literal[
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "artifact",
    "status",
    "data",
]
EventPhase = Literal["commentary", "final_answer"]
InteractionKind = Literal["approval", "structured_input"]
ContinuationKind = Literal[
    "graph_checkpoint",
    "invocation_resume",
    "thread_resume",
    "task_resume",
]


def _normalize_json_integer(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


JsonInteger: TypeAlias = Annotated[
    int,
    BeforeValidator(_normalize_json_integer),
    Field(strict=True),
]


class _CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceRef(_CanonicalModel):
    framework: Framework
    protocol: SourceProtocol | None = None
    native_event_id: str | None = None
    native_cursor: str | None = None
    native_run_id: str | None = None
    native_item_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class EventEnvelope(_CanonicalModel):
    schema_version: Literal[2]
    event_id: str = Field(min_length=1)
    seq: JsonInteger = Field(ge=0)
    timestamp: float
    run_id: str = Field(min_length=1)
    run_seq: JsonInteger | None = Field(default=None, ge=0)
    scope_id: str = Field(min_length=1)
    parent_scope_id: str | None = Field(default=None, min_length=1)
    source: SourceRef


class ErrorInfo(_CanonicalModel):
    code: str = Field(min_length=1)
    message: str | None = None
    source: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    item_id: str | None = None
    source_ref: SourceRef | None = None


class OutputRef(_CanonicalModel):
    scope_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    part_id: str | None = Field(default=None, min_length=1)


class ApprovalRequest(_CanonicalModel):
    request_type: Literal["approval"] = "approval"
    call_id: str | None = None
    kind: str = Field(min_length=1)
    detail: JsonValue = None


class StructuredInputRequest(_CanonicalModel):
    request_type: Literal["structured_input"] = "structured_input"
    prompt: str | None = None
    schema_: dict[str, JsonValue] = Field(alias="schema")


InteractionRequest: TypeAlias = Annotated[
    Union[ApprovalRequest, StructuredInputRequest],
    Field(discriminator="request_type"),
]


class ApprovalResponse(_CanonicalModel):
    response_type: Literal["approval"] = "approval"
    decision: Literal["approved", "rejected", "canceled"]
    data: JsonValue = None


class StructuredInputResponse(_CanonicalModel):
    response_type: Literal["structured_input"] = "structured_input"
    data: JsonValue


InteractionResponse: TypeAlias = Annotated[
    Union[ApprovalResponse, StructuredInputResponse],
    Field(discriminator="response_type"),
]


class RunStarted(EventEnvelope):
    event_type: Literal["run.started"] = "run.started"
    status: Literal["running"]


class RunProgress(EventEnvelope):
    event_type: Literal["run.progress"] = "run.progress"
    status: Literal["running"]
    progress: float | None = None
    message: str | None = None


class RunInterrupted(EventEnvelope):
    event_type: Literal["run.interrupted"] = "run.interrupted"
    status: Literal["interrupted"]
    reason: str | None = None
    interaction_id: str | None = Field(default=None, min_length=1)
    continuation_id: str | None = Field(default=None, min_length=1)


class RunCompleted(EventEnvelope):
    event_type: Literal["run.completed"] = "run.completed"
    status: Literal["completed"]
    output_refs: tuple[OutputRef, ...] = Field(strict=False)


class RunFailed(EventEnvelope):
    event_type: Literal["run.failed"] = "run.failed"
    status: Literal["failed"]
    error: ErrorInfo


class RunCanceled(EventEnvelope):
    event_type: Literal["run.canceled"] = "run.canceled"
    status: Literal["canceled"]
    reason: str | None = None


class ItemStarted(EventEnvelope):
    event_type: Literal["item.started"] = "item.started"
    item_id: str = Field(min_length=1)
    item_kind: ItemKind
    phase: EventPhase | None = None
    initial: ContentSnapshot | None = None


class ItemUpdated(EventEnvelope):
    event_type: Literal["item.updated"] = "item.updated"
    item_id: str = Field(min_length=1)
    item_kind: ItemKind
    op: Literal["append", "replace"]
    update: ContentUpdate


class ItemSnapshotReplaced(EventEnvelope):
    """Atomically replace every ordered part of an open item."""

    event_type: Literal["item.snapshot_replaced"] = "item.snapshot_replaced"
    item_id: str = Field(min_length=1)
    item_kind: ItemKind
    snapshot: ContentSnapshot


class ItemCompleted(EventEnvelope):
    event_type: Literal["item.completed"] = "item.completed"
    item_id: str = Field(min_length=1)
    item_kind: ItemKind
    snapshot: ContentSnapshot


class ItemFailed(EventEnvelope):
    event_type: Literal["item.failed"] = "item.failed"
    item_id: str = Field(min_length=1)
    item_kind: ItemKind
    error: ErrorInfo


class InteractionRequested(EventEnvelope):
    event_type: Literal["interaction.requested"] = "interaction.requested"
    interaction_id: str = Field(min_length=1)
    interaction_kind: InteractionKind
    request: InteractionRequest

    @model_validator(mode="after")
    def _matching_request_kind(self) -> "InteractionRequested":
        if self.interaction_kind != self.request.request_type:
            raise ValueError("interaction_kind must match request_type")
        return self


class InteractionResolved(EventEnvelope):
    event_type: Literal["interaction.resolved"] = "interaction.resolved"
    interaction_id: str = Field(min_length=1)
    interaction_kind: InteractionKind
    response: InteractionResponse

    @model_validator(mode="after")
    def _matching_response_kind(self) -> "InteractionResolved":
        if self.interaction_kind != self.response.response_type:
            raise ValueError("interaction_kind must match response_type")
        return self


class ContinuationCreated(EventEnvelope):
    event_type: Literal["continuation.created"] = "continuation.created"
    continuation_id: str = Field(min_length=1)
    continuation_kind: ContinuationKind
    resumable: bool
    ref: dict[str, JsonValue]


class ContinuationResumed(EventEnvelope):
    event_type: Literal["continuation.resumed"] = "continuation.resumed"
    continuation_id: str = Field(min_length=1)
    continuation_kind: ContinuationKind
    resume_attempt_id: str = Field(min_length=1)


class ContextCompactionStarted(EventEnvelope):
    event_type: Literal["context.compaction.started"] = "context.compaction.started"
    trigger: str = Field(min_length=1)


class ContextCompactionCompleted(EventEnvelope):
    event_type: Literal["context.compaction.completed"] = "context.compaction.completed"
    trigger: str = Field(min_length=1)
    compacted_until_seq: JsonInteger = Field(ge=0)


class UsageReported(EventEnvelope):
    event_type: Literal["usage.reported"] = "usage.reported"
    input_tokens: JsonInteger = Field(ge=0)
    output_tokens: JsonInteger = Field(ge=0)
    total_tokens: JsonInteger = Field(ge=0)
    cached_tokens: JsonInteger = Field(default=0, ge=0)
    reasoning_tokens: JsonInteger = Field(default=0, ge=0)


RuntimeEvent: TypeAlias = Annotated[
    Union[
        RunStarted,
        RunProgress,
        RunInterrupted,
        RunCompleted,
        RunFailed,
        RunCanceled,
        ItemStarted,
        ItemUpdated,
        ItemSnapshotReplaced,
        ItemCompleted,
        ItemFailed,
        InteractionRequested,
        InteractionResolved,
        ContinuationCreated,
        ContinuationResumed,
        ContextCompactionStarted,
        ContextCompactionCompleted,
        UsageReported,
    ],
    Field(discriminator="event_type"),
]

_RUNTIME_EVENT_ADAPTER: TypeAdapter[RuntimeEvent] = TypeAdapter(RuntimeEvent)
_RUNTIME_EVENT_MODELS = cast(
    tuple[type[EventEnvelope], ...],
    get_args(get_args(RuntimeEvent)[0]),
)


def _event_type_for_model(model: type[EventEnvelope]) -> str:
    literal_values = get_args(model.model_fields["event_type"].annotation)
    if len(literal_values) != 1 or not isinstance(literal_values[0], str):
        raise TypeError(f"{model.__name__}.event_type must contain one string literal")
    return literal_values[0]


ALL_EVENT_TYPES = frozenset(_event_type_for_model(model) for model in _RUNTIME_EVENT_MODELS)


def parse_runtime_event(data: object) -> RuntimeEvent:
    """Validate a canonical event from a JSON string/bytes or Python value."""

    if isinstance(data, (str, bytes, bytearray)):
        return _RUNTIME_EVENT_ADAPTER.validate_json(data)
    return _RUNTIME_EVENT_ADAPTER.validate_python(data)


def dump_runtime_event(event: RuntimeEvent) -> dict[str, JsonValue]:
    """Serialize a canonical event to a JSON-compatible dictionary."""

    return cast(
        dict[str, JsonValue],
        _RUNTIME_EVENT_ADAPTER.dump_python(
            event,
            mode="json",
            by_alias=True,
            exclude_none=True,
        ),
    )


__all__ = [
    "ALL_EVENT_TYPES",
    "ApprovalRequest",
    "ApprovalResponse",
    "ContextCompactionCompleted",
    "ContextCompactionStarted",
    "ContinuationCreated",
    "ContinuationKind",
    "ContinuationResumed",
    "ErrorInfo",
    "EventEnvelope",
    "EventPhase",
    "InteractionKind",
    "InteractionRequest",
    "InteractionRequested",
    "InteractionResolved",
    "InteractionResponse",
    "ItemCompleted",
    "ItemFailed",
    "ItemKind",
    "ItemSnapshotReplaced",
    "ItemStarted",
    "ItemUpdated",
    "JsonInteger",
    "OutputRef",
    "RunCanceled",
    "RunCompleted",
    "RunFailed",
    "RunInterrupted",
    "RunProgress",
    "RunStarted",
    "RuntimeEvent",
    "SourceProtocol",
    "SourceRef",
    "StructuredInputRequest",
    "StructuredInputResponse",
    "UsageReported",
    "dump_runtime_event",
    "parse_runtime_event",
]
