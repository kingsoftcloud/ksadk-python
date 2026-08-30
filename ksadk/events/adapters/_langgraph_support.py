"""LangGraph adapter 的常量、状态 dataclass 与映射辅助（纯移动自 adapters.langgraph，行为不变）。"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, cast

from langchain_core.messages import AIMessage
from pydantic import JsonValue

from ksadk.events.canonical import (
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    ContentSnapshot,
    ContentValue,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_part_id,
    stable_scope_id,
)

# Native content-block types that carry a tool call identity.
_TOOL_CALL_BLOCKS = frozenset(
    "tool_call tool_call_chunk server_tool_call server_tool_call_chunk".split()
)
# Native tool-call delta shapes accepted without a payload translation.
_TOOL_DELTA_TYPES = frozenset(
    "tool_call tool_call_chunk tool_call-delta server_tool_call server_tool_call_chunk".split()
)
# Lifecycle native types that close the nested scope without a run-progress event.
_LIFECYCLE_QUIET_TYPES = frozenset({"interrupted"})


def _fail(code: str, field_name: str, message: str) -> NoReturn:
    raise LangGraphMappingError(code, field_name, message)


class LangGraphMappingError(ValueError):
    """A LangGraph v3 event violates the native identity contract."""

    def __init__(self, code: str, field_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.source = "langgraph"


@dataclass
class LangGraphAdapterContext:
    """Invocation facts and deterministic pre-store reducer ordering.

    ``graph_run_id`` is supplied by the runner invocation/config because the
    in-process ProtocolEvent envelope identifies LLM runs but not the enclosing
    graph run. Allocated ``seq`` values are placeholders only; RuntimeEventStore
    remains the canonical session sequence allocator.
    """

    run_id: str
    graph_run_id: str
    initial_seq: int = 0
    checkpoint_ref: Mapping[str, str] | None = None
    _next_seq: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.run_id = _required_string(self.run_id, "runtime run_id")
        self.graph_run_id = _required_string(self.graph_run_id, "graph_run_id")
        if self.initial_seq < 0:
            raise ValueError("LangGraph initial_seq must be non-negative")
        self._next_seq = self.initial_seq
        if self.checkpoint_ref is not None:
            self.checkpoint_ref = dict(self.checkpoint_ref)

    def allocate_placeholder_seq(self) -> int:
        value = self._next_seq
        self._next_seq += 1
        return value


@dataclass
class _Frame:
    """Per-ProtocolEvent routing facts shared by all method lanes."""

    namespace: tuple[str, ...]
    scope_id: str
    parent_scope_id: str | None
    source_seq: int
    native_event_id: str | None
    occurrence_key: str
    timestamp: float


@dataclass
class _ItemLane:
    item_id: str
    item_kind: Literal["message", "reasoning", "tool_call", "tool_result"]
    phase: Literal["commentary", "final_answer"]
    native_item_id: str
    parts: dict[int, ContentValue] = field(default_factory=dict)
    completed: bool = False


@dataclass
class _MessageState:
    scope_id: str
    parent_scope_id: str | None
    llm_run_id: str
    message_id: str
    node: str
    lanes: dict[str, _ItemLane] = field(default_factory=dict)
    block_lanes: dict[int, str] = field(default_factory=dict)
    finished_blocks: set[int] = field(default_factory=set)


@dataclass
class _ToolState:
    scope_id: str
    parent_scope_id: str | None
    call_id: str
    name: str
    item_id: str


@dataclass
class _LifecycleState:
    scope_id: str
    parent_scope_id: str | None
    item_id: str
    namespace: tuple[str, ...]


def _map_data_channel(
    *,
    context: LangGraphAdapterContext,
    frame: _Frame,
    method: str,
    source: SourceRef,
    value: Any,
) -> tuple[RuntimeEvent, ...]:
    env = _envelope(context, frame.occurrence_key, frame.timestamp)
    item_id = stable_item_id("langgraph", frame.scope_id, "channel", method, frame.source_seq)
    part_id = _part_id(item_id, "channel", method)
    content = DataContent(part_id=part_id, data=_json_value(value))
    envelope = lambda event_type: env(  # noqa: E731
        frame.scope_id, frame.parent_scope_id, item_id, event_type, part_id, source
    )
    return (
        ItemStarted(
            **envelope("item.started"),
            item_id=item_id,
            item_kind="data",
            phase="commentary",
        ),
        ItemCompleted(
            **envelope("item.completed"),
            item_id=item_id,
            item_kind="data",
            snapshot=ContentSnapshot(parts=(content,)),
        ),
    )


def _new_lane(
    state: _MessageState,
    item_kind: Literal["message", "reasoning", "tool_call", "tool_result"],
    native_item_id: str,
) -> _ItemLane:
    if item_kind == "message":
        item_id = stable_item_id(
            "langgraph", state.scope_id, "message", state.llm_run_id, state.message_id
        )
        phase: Literal["commentary", "final_answer"] = "final_answer"
    elif item_kind == "reasoning":
        item_id = stable_item_id(
            "langgraph", state.scope_id, "reasoning", state.llm_run_id, state.message_id
        )
        phase = "commentary"
    elif item_kind == "tool_call":
        item_id = stable_item_id(
            "langgraph", state.scope_id, "tool_call", state.llm_run_id, native_item_id
        )
        phase = "commentary"
    else:
        # Provider-executed results on the messages channel are semantically
        # distinct from locally executed results on the tools channel.
        item_id = stable_item_id(
            "langgraph",
            state.scope_id,
            "provider_tool_result",
            state.llm_run_id,
            state.message_id,
            native_item_id,
        )
        phase = "commentary"
    return _ItemLane(
        item_id=item_id,
        item_kind=item_kind,
        phase=phase,
        native_item_id=native_item_id,
    )


def _lane_for_content(
    state: _MessageState,
    index: int,
    content: Mapping[str, Any],
) -> tuple[_ItemLane, bool]:
    if index in state.block_lanes:
        _fail(
            "content_block_already_started",
            "content-block-start.index",
            f"LangGraph content block {index} started twice",
        )
    block_type = _required_string(content.get("type"), "content block type")
    if block_type in _TOOL_CALL_BLOCKS:
        call_id = _required_string(content.get("id"), "tool_call.id")
        lane_key, item_kind, native_item_id = f"tool_call:{call_id}", "tool_call", call_id
    elif block_type == "server_tool_result":
        call_id = _required_string(content.get("tool_call_id"), "server_tool_result.tool_call_id")
        lane_key, item_kind, native_item_id = (
            f"provider_tool_result:{call_id}",
            "tool_result",
            call_id,
        )
    elif block_type == "text":
        lane_key, item_kind, native_item_id = "message", "message", state.message_id
    elif block_type == "reasoning":
        lane_key, item_kind, native_item_id = "reasoning", "reasoning", state.message_id
    else:
        _fail(
            "unsupported_content_block",
            "content.type",
            f"Unsupported LangGraph content block type: {block_type}",
        )
    lane = state.lanes.get(lane_key)
    created = lane is None
    if lane is None:
        lane = _new_lane(state, item_kind, native_item_id)
        state.lanes[lane_key] = lane
    state.block_lanes[index] = lane_key
    return lane, created


def _lane_for_index(state: _MessageState, index: int) -> _ItemLane:
    lane_key = state.block_lanes.get(index)
    if lane_key is None:
        _fail(
            "content_block_not_started",
            "content block index",
            f"LangGraph content block {index} mutated before start",
        )
    return state.lanes[lane_key]


def _lane_source(source: SourceRef, lane: _ItemLane) -> SourceRef:
    update: dict[str, Any] = {"native_item_id": lane.native_item_id}
    if lane.item_kind == "tool_result":
        update["metadata"] = {**source.metadata, "tool_semantic": "provider_result"}
    return source.model_copy(update=update)


def _lane_started(
    lane_env: Callable[..., dict[str, Any]],
    lane: _ItemLane,
) -> ItemStarted:
    return ItemStarted(
        **lane_env(lane, "item.started", lane.item_kind),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        phase=lane.phase,
    )


def _lane_updated(
    lane_env: Callable[..., dict[str, Any]],
    lane: _ItemLane,
    update: ContentValue,
    op: Literal["append", "replace"],
    ordinal: int,
) -> ItemUpdated:
    return ItemUpdated(
        **lane_env(lane, "item.updated", update.part_id, ordinal),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        op=op,
        update=update,
    )


def _lane_completed(
    lane_env: Callable[..., dict[str, Any]],
    lane: _ItemLane,
) -> ItemCompleted:
    return ItemCompleted(
        **lane_env(lane, "item.completed", "snapshot"),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        snapshot=ContentSnapshot(parts=tuple(lane.parts[index] for index in sorted(lane.parts))),
    )


def _map_whole_message(
    *,
    payload: AIMessage,
    metadata: Mapping[str, Any],
    context: LangGraphAdapterContext,
    frame: _Frame,
    node: str,
) -> tuple[RuntimeEvent, ...]:
    message_id = _required_string(payload.id, "whole message.id")
    llm_run_id = _optional_string(metadata.get("run_id")) or context.graph_run_id
    state = _MessageState(
        scope_id=frame.scope_id,
        parent_scope_id=frame.parent_scope_id,
        llm_run_id=llm_run_id,
        message_id=message_id,
        node=node,
    )
    source = _source_ref(
        channel="messages",
        native_run_id=state.llm_run_id,
        native_item_id=state.message_id,
        source_seq=frame.source_seq,
        native_event_id=frame.native_event_id,
        extra={
            "graph_run_id": context.graph_run_id,
            "namespace": list(frame.namespace),
            "node": state.node,
        },
    )
    env = _envelope(context, frame.occurrence_key, frame.timestamp)

    content = payload.content
    blocks: Sequence[Any]
    if isinstance(content, str):
        # An empty whole-message string carries no text block. Tool-only
        # messages therefore avoid a phantom final-answer lane; if there are
        # no tool calls either, the fallback below preserves one empty message.
        blocks = () if content == "" else ({"type": "text", "text": content},)
    elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        blocks = content
    else:
        _fail(
            "unsupported_whole_message",
            "whole message.content",
            "LangGraph whole message content must be text or typed blocks",
        )

    for index, value in enumerate(blocks):
        block = _mapping(value, f"whole message.content[{index}]")
        lane, _ = _lane_for_content(state, index, block)
        if lane.item_kind == "tool_call":
            lane.parts[index] = _tool_call_snapshot(lane.item_id, index, block)
        elif lane.item_kind == "tool_result":
            lane.parts[index] = _server_tool_result_snapshot(lane.item_id, index, block)
        else:
            lane.parts[index] = _text_block_snapshot(lane.item_id, index, block)

    tool_calls = getattr(payload, "tool_calls", ())
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for offset, value in enumerate(tool_calls, start=len(blocks)):
            call = _mapping(value, f"whole message.tool_calls[{offset - len(blocks)}]")
            normalized_call = {
                "type": "tool_call",
                "id": call.get("id"),
                "name": call.get("name"),
                "args": call.get("args"),
            }
            call_id = _required_string(normalized_call["id"], "tool_call.id")
            if f"tool_call:{call_id}" in state.lanes:
                continue
            lane, _ = _lane_for_content(state, offset, normalized_call)
            lane.parts[offset] = _tool_call_snapshot(lane.item_id, offset, normalized_call)

    if not state.lanes:
        state.lanes["message"] = _new_lane(state, "message", state.message_id)

    emitted: list[RuntimeEvent] = []
    for lane in state.lanes.values():
        emitted.append(
            ItemStarted(
                **env(
                    state.scope_id,
                    state.parent_scope_id,
                    lane.item_id,
                    "item.started",
                    lane.item_kind,
                    _lane_source(source, lane),
                ),
                item_id=lane.item_id,
                item_kind=lane.item_kind,
                phase=lane.phase,
            )
        )
        lane.completed = True
        emitted.append(
            ItemCompleted(
                **env(
                    state.scope_id,
                    state.parent_scope_id,
                    lane.item_id,
                    "item.completed",
                    "snapshot",
                    _lane_source(source, lane),
                ),
                item_id=lane.item_id,
                item_kind=lane.item_kind,
                snapshot=ContentSnapshot(
                    parts=tuple(lane.parts[index] for index in sorted(lane.parts))
                ),
            )
        )
    return tuple(emitted)


def _envelope(
    context: LangGraphAdapterContext,
    occurrence_key: str,
    timestamp: float,
) -> Callable[..., dict[str, Any]]:
    def env(
        scope_id: str,
        parent_scope_id: str | None,
        item_id: str,
        event_type: str,
        part_id: str,
        source: SourceRef,
        ordinal: int = 0,
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                "langgraph",
                scope_id,
                item_id,
                event_type,
                part_id,
                occurrence_key,
                ordinal,
            ),
            "seq": context.allocate_placeholder_seq(),
            "timestamp": timestamp,
            "run_id": context.run_id,
            "scope_id": scope_id,
            "parent_scope_id": parent_scope_id,
            "source": source,
        }

    return env


def _source_ref(
    *,
    channel: str,
    native_run_id: str | None,
    native_item_id: str | None,
    source_seq: int,
    native_event_id: str | None,
    extra: Mapping[str, JsonValue] | None = None,
) -> SourceRef:
    metadata: dict[str, JsonValue] = {
        "stream_version": "v3",
        "channel": channel,
        "seq_semantics": "source_cursor",
    }
    if extra:
        metadata.update(extra)
    return SourceRef(
        framework="langgraph",
        native_event_id=native_event_id,
        native_cursor=str(source_seq),
        native_run_id=native_run_id,
        native_item_id=native_item_id,
        metadata=metadata,
    )


def _message_data(value: Any) -> tuple[Any, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        _fail(
            "invalid_messages_data",
            "params.data",
            "LangGraph messages data must be (MessagesData, metadata)",
        )
    return (
        value[0],
        _mapping(value[1], "params.data[1]"),
    )


def _text_block_snapshot(
    item_id: str,
    index: int,
    content: Mapping[str, Any],
) -> TextContent:
    block_type = _required_string(content.get("type"), "content block type")
    part_id = _part_id(item_id, "content-block", block_type, index)
    if block_type == "text":
        field_name = "text"
    elif block_type == "reasoning":
        field_name = "reasoning"
    else:
        _fail(
            "unsupported_content_block",
            "content.type",
            f"Unsupported LangGraph text-like block type: {block_type}",
        )
    # langchain-protocol 0.0.18 makes the reasoning body optional on both
    # ReasoningContentBlock shapes. Empty is therefore an authoritative native
    # snapshot; later deltas may append and finish may replace it again.
    text = content.get(field_name, "") if block_type == "reasoning" else content.get(field_name)
    if not isinstance(text, str):
        _fail(
            "invalid_content_block",
            f"content.{field_name}",
            f"LangGraph {block_type} block requires string {field_name}",
        )
    return TextContent(part_id=part_id, text=text)


def _text_block_delta(
    item_id: str,
    index: int,
    delta: Mapping[str, Any],
    item_kind: Literal["message", "reasoning", "tool_call", "tool_result"],
) -> TextContent:
    delta_type = _required_string(delta.get("type"), "content delta type")
    if item_kind == "message" and delta_type == "text-delta":
        block_type, field_name = "text", "text"
    elif item_kind == "reasoning" and delta_type == "reasoning-delta":
        block_type, field_name = "reasoning", "reasoning"
    else:
        _fail(
            "unsupported_content_delta",
            "delta.type",
            f"Unsupported LangGraph content delta type: {delta_type}",
        )
    part_id = _part_id(item_id, "content-block", block_type, index)
    text = delta.get(field_name)
    if not isinstance(text, str):
        _fail(
            "invalid_content_delta",
            f"delta.{field_name}",
            f"LangGraph {delta_type} requires string {field_name}",
        )
    return TextContent(part_id=part_id, text=text)


def _validate_tool_delta(delta: Mapping[str, Any]) -> None:
    delta_type = _required_string(delta.get("type"), "content delta type")
    if delta_type == "block-delta":
        fields = _mapping(delta.get("fields"), "block-delta.fields")
        if _required_string(fields.get("type"), "block-delta.fields.type") in _TOOL_CALL_BLOCKS:
            return
    if delta_type in _TOOL_DELTA_TYPES:
        return
    _fail(
        "unsupported_content_delta",
        "delta.type",
        f"Unsupported LangGraph tool call delta type: {delta_type}",
    )


def _tool_call_snapshot(
    item_id: str,
    index: int,
    content: Mapping[str, Any],
) -> ToolCallContent:
    block_type = _required_string(content.get("type"), "content block type")
    if block_type not in _TOOL_CALL_BLOCKS:
        _fail(
            "unsupported_content_block",
            "content.type",
            f"Expected LangGraph tool call block, got: {block_type}",
        )
    call_id = _required_string(content.get("id"), "tool_call.id")
    name = _required_string(content.get("name"), "tool_call.name")
    return ToolCallContent(
        part_id=_part_id(item_id, "tool-call", call_id, index),
        call_id=call_id,
        name=name,
        arguments=_json_value(content.get("args")),
    )


def _server_tool_result_snapshot(
    item_id: str,
    index: int,
    content: Mapping[str, Any],
) -> ToolResultContent:
    block_type = _required_string(content.get("type"), "content block type")
    if block_type != "server_tool_result":
        _fail(
            "unsupported_content_block",
            "content.type",
            f"Expected LangGraph server tool result block, got: {block_type}",
        )
    call_id = _required_string(content.get("tool_call_id"), "server_tool_result.tool_call_id")
    status = _required_string(content.get("status"), "server_tool_result.status")
    if status not in {"success", "error"}:
        _fail(
            "invalid_content_block",
            "server_tool_result.status",
            f"Unsupported LangGraph server tool result status: {status}",
        )
    return ToolResultContent(
        part_id=_part_id(item_id, "provider-tool-result", call_id, index),
        call_id=call_id,
        result=_json_value(content.get("output")),
        is_error=status == "error",
    )


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return cast(JsonValue, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol data contains a non-finite float",
            )
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            _fail(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol data object keys must be strings",
            )
        return cast(
            JsonValue,
            {key: _json_value(item) for key, item in value.items()},
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return cast(JsonValue, [_json_value(item) for item in value])
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except Exception as exc:
            raise LangGraphMappingError(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol model could not be serialized to JSON",
            ) from exc
        if dumped is value:
            _fail(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol model returned itself from model_dump",
            )
        return _json_value(dumped)
    _fail(
        "non_json_protocol_data",
        "ProtocolEvent.params.data",
        f"LangGraph protocol data is not stably JSON serializable: {type(value).__name__}",
    )


def _scope_id(graph_run_id: str, namespace: tuple[str, ...]) -> str:
    return stable_scope_id("langgraph", graph_run_id, _namespace_identity(namespace))


def _parent_scope_id(graph_run_id: str, namespace: tuple[str, ...]) -> str | None:
    if not namespace:
        return None
    return _scope_id(graph_run_id, namespace[:-1])


def _namespace(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail(
            "invalid_namespace",
            "namespace",
            "LangGraph namespace must be an ordered string sequence",
        )
    return tuple(_required_string(component, "namespace component") for component in value)


def _namespace_identity(namespace: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "length": len(namespace),
            "components": [{"type": "string", "value": component} for component in namespace],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _part_id(item_id: str, *components: str | int) -> str:
    return stable_part_id("langgraph", item_id, *components)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(
            "invalid_event_shape",
            field_name,
            f"LangGraph {field_name} must be an object",
        )
    return value


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(
            "missing_native_identity",
            field_name,
            f"LangGraph {field_name} must be a non-empty string",
        )
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _block_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(
            "invalid_block_index",
            "index",
            "LangGraph content block index must be a non-negative integer",
        )
    return int(value)


def _source_seq(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(
            "missing_source_cursor",
            "seq",
            "LangGraph ProtocolEvent requires its non-negative root mux seq",
        )
    return int(value)


def _protocol_timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(
            "invalid_timestamp",
            "params.timestamp",
            "LangGraph ProtocolEvent timestamp must be epoch milliseconds",
        )
    return float(value) / 1000.0


def _interrupt_reason(interrupts: Any) -> str | None:
    if not isinstance(interrupts, Sequence) or isinstance(interrupts, (str, bytes)):
        _fail(
            "invalid_interrupts",
            "params.interrupts",
            "LangGraph interrupts must be a sequence",
        )
    if not interrupts:
        return None
    first = interrupts[0]
    if isinstance(first, Mapping):
        value = first.get("value")
    else:
        value = getattr(first, "value", None)
    return value if isinstance(value, str) and value else str(first)


__all__ = [
    "LangGraphAdapterContext",
    "LangGraphMappingError",
]
