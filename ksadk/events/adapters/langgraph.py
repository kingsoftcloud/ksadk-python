"""LangGraph 1.2.x raw v3 ProtocolEvents to RuntimeEvent schema v2."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.stream import AsyncGraphRunStream
from pydantic import JsonValue

from ksadk.events.canonical import (
    ApprovalRequest,
    ContinuationCreated,
    ErrorInfo,
    InteractionRequested,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    RunInterrupted,
    RunProgress,
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


class LangGraphEventAdapter:
    """Consume the lossless raw log exposed by ``AsyncGraphRunStream``."""

    def __init__(self) -> None:
        self._messages: dict[tuple[str, str], _MessageState] = {}
        self._tools: dict[tuple[str, str], _ToolState] = {}
        self._lifecycles: dict[str, _LifecycleState] = {}

    async def stream_run(
        self,
        run: AsyncGraphRunStream,
        context: LangGraphAdapterContext,
    ) -> AsyncIterator[RuntimeEvent]:
        """Map a public v3 run's raw ProtocolEvent log in source order."""

        try:
            async for native_event in run:
                for canonical in self.map_protocol_event(native_event, context):
                    yield canonical
            if self._messages or self._tools or self._lifecycles:
                open_runs = ", ".join(sorted(state.llm_run_id for state in self._messages.values()))
                open_calls = ", ".join(sorted(state.call_id for state in self._tools.values()))
                open_scopes = ", ".join(sorted(self._lifecycles))
                if self._messages and not self._tools and not self._lifecycles:
                    code = "open_messages_at_stream_end"
                    field_name = "messages metadata.run_id"
                elif self._tools and not self._messages and not self._lifecycles:
                    code = "open_tools_at_stream_end"
                    field_name = "tools tool_call_id"
                elif self._lifecycles and not self._messages and not self._tools:
                    code = "open_lifecycle_at_stream_end"
                    field_name = "lifecycle namespace"
                else:
                    code = "open_items_at_stream_end"
                    field_name = "ProtocolEvent"
                raise LangGraphMappingError(
                    code,
                    field_name,
                    "LangGraph stream ended with open native items: "
                    f"message_runs=[{open_runs}], tool_calls=[{open_calls}], "
                    f"lifecycle_scopes=[{open_scopes}]",
                )
        finally:
            await run.abort()

    def map_protocol_event(
        self,
        raw_event: Mapping[str, Any],
        context: LangGraphAdapterContext,
    ) -> tuple[RuntimeEvent, ...]:
        """Map one real ProtocolEvent yielded by ``AsyncGraphRunStream``."""

        event = _mapping(raw_event, "ProtocolEvent")
        if event.get("type") != "event":
            raise LangGraphMappingError(
                "invalid_protocol_event",
                "type",
                "LangGraph ProtocolEvent.type must be 'event'",
            )
        method = _required_string(event.get("method"), "ProtocolEvent.method")
        params = _mapping(event.get("params"), "ProtocolEvent.params")
        namespace = _namespace(params.get("namespace"))
        scope_id = _scope_id(context.graph_run_id, namespace)
        parent_scope_id = _parent_scope_id(context.graph_run_id, namespace)
        source_seq = _source_seq(event.get("seq"))
        native_event_id = _optional_string(event.get("event_id"))
        timestamp = _protocol_timestamp(params.get("timestamp"))
        occurrence_key = native_event_id or f"seq:{source_seq}"

        if method == "messages":
            return self._map_message_event(
                params=params,
                context=context,
                namespace=namespace,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                source_seq=source_seq,
                native_event_id=native_event_id,
                occurrence_key=occurrence_key,
                timestamp=timestamp,
            )

        if method == "tools":
            return self._map_tool_event(
                params=params,
                context=context,
                namespace=namespace,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                source_seq=source_seq,
                native_event_id=native_event_id,
                occurrence_key=occurrence_key,
                timestamp=timestamp,
            )

        if method == "lifecycle":
            return self._map_lifecycle_event(
                params=params,
                context=context,
                emitter_namespace=namespace,
                source_seq=source_seq,
                native_event_id=native_event_id,
                occurrence_key=occurrence_key,
                timestamp=timestamp,
            )

        source = _protocol_source(
            context=context,
            method=method,
            namespace=namespace,
            source_seq=source_seq,
            native_event_id=native_event_id,
        )
        if "data" not in params:
            raise LangGraphMappingError(
                "missing_protocol_data",
                "ProtocolEvent.params.data",
                f"LangGraph {method} event requires params.data",
            )
        interrupts = params.get("interrupts", ())
        if method == "values" and interrupts:
            return self._map_interrupt(
                context=context,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                source=source,
                timestamp=timestamp,
                occurrence_key=occurrence_key,
                interrupts=interrupts,
            )
        return _map_data_channel(
            context=context,
            scope_id=scope_id,
            parent_scope_id=parent_scope_id,
            method=method,
            source_seq=source_seq,
            source=source,
            timestamp=timestamp,
            occurrence_key=occurrence_key,
            value=params["data"],
        )

    def _map_lifecycle_event(
        self,
        *,
        params: Mapping[str, Any],
        context: LangGraphAdapterContext,
        emitter_namespace: tuple[str, ...],
        source_seq: int,
        native_event_id: str | None,
        occurrence_key: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        payload = _mapping(params.get("data"), "lifecycle data")
        native_type = _required_string(payload.get("event"), "lifecycle event")
        target_namespace = _namespace(payload.get("namespace"))
        if not target_namespace:
            raise LangGraphMappingError(
                "unsupported_root_lifecycle",
                "lifecycle namespace",
                "LangGraph v3 lifecycle events must identify a nested target scope",
            )
        scope_id = _scope_id(context.graph_run_id, target_namespace)
        parent_scope_id = _parent_scope_id(context.graph_run_id, target_namespace)
        item_id = stable_item_id(
            "langgraph",
            scope_id,
            "lifecycle",
            _namespace_identity(target_namespace),
        )
        source = _lifecycle_source(
            context=context,
            emitter_namespace=emitter_namespace,
            target_namespace=target_namespace,
            source_seq=source_seq,
            native_event_id=native_event_id,
        )
        part = DataContent(
            part_id=_part_id(item_id, "lifecycle-status"),
            data=_json_value(payload),
        )

        if native_type == "started":
            if scope_id in self._lifecycles:
                raise LangGraphMappingError(
                    "lifecycle_already_started",
                    "lifecycle namespace",
                    "LangGraph nested lifecycle started twice",
                )
            start_state = _LifecycleState(
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                item_id=item_id,
                namespace=target_namespace,
            )
            self._lifecycles[scope_id] = start_state
            return (
                _lifecycle_progress(
                    native_type=native_type,
                    context=context,
                    state=start_state,
                    occurrence_key=occurrence_key,
                    source=source,
                    timestamp=timestamp,
                ),
                ItemStarted(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part.part_id,
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=item_id,
                    item_kind="status",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(part,)),
                ),
            )

        terminal_state = self._lifecycles.get(scope_id)
        if terminal_state is None:
            raise LangGraphMappingError(
                "lifecycle_not_started",
                "lifecycle namespace",
                "LangGraph nested lifecycle terminated before started",
            )
        if native_type == "failed":
            del self._lifecycles[scope_id]
            message = str(payload.get("error") or "LangGraph subgraph failed")
            return (
                ItemFailed(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=item_id,
                        event_type="item.failed",
                        part_id=part.part_id,
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=item_id,
                    item_kind="status",
                    error=ErrorInfo(
                        code="langgraph_subgraph_failed",
                        message=message,
                        source="langgraph",
                        scope_id=scope_id,
                        item_id=item_id,
                        source_ref=source,
                    ),
                ),
            )
        if native_type not in {"completed", "interrupted", "drained"}:
            raise LangGraphMappingError(
                "unsupported_lifecycle_event",
                "lifecycle event",
                f"Unsupported LangGraph lifecycle event: {native_type}",
            )
        del self._lifecycles[scope_id]
        progress = (
            _lifecycle_progress(
                native_type=native_type,
                context=context,
                state=terminal_state,
                occurrence_key=occurrence_key,
                source=source,
                timestamp=timestamp,
            )
            if native_type in {"completed", "drained"}
            else None
        )
        completed = ItemCompleted(
            **_envelope(
                context=context,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                item_id=item_id,
                event_type="item.completed",
                part_id=part.part_id,
                occurrence_key=occurrence_key,
                source=source,
                timestamp=timestamp,
            ),
            item_id=item_id,
            item_kind="status",
            snapshot=ContentSnapshot(parts=(part,)),
        )
        if progress is not None:
            return (progress, completed)
        return (completed,)

    def _map_tool_event(
        self,
        *,
        params: Mapping[str, Any],
        context: LangGraphAdapterContext,
        namespace: tuple[str, ...],
        scope_id: str,
        parent_scope_id: str | None,
        source_seq: int,
        native_event_id: str | None,
        occurrence_key: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        payload = _mapping(params.get("data"), "tools data")
        native_type = _required_string(payload.get("event"), "tools event")
        call_id = _required_string(payload.get("tool_call_id"), "tools tool_call_id")
        state_key = (scope_id, call_id)
        source = _tool_source(
            context=context,
            namespace=namespace,
            call_id=call_id,
            source_seq=source_seq,
            native_event_id=native_event_id,
        )

        if native_type == "tool-started":
            if state_key in self._tools:
                raise LangGraphMappingError(
                    "tool_already_started",
                    "tools tool_call_id",
                    f"LangGraph tool call {call_id!r} started twice",
                )
            name = _required_string(payload.get("tool_name"), "tools tool_name")
            item_id = stable_item_id("langgraph", scope_id, "tool_result", call_id)
            self._tools[state_key] = _ToolState(
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                call_id=call_id,
                name=name,
                item_id=item_id,
            )
            return (
                ItemStarted(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id="tool-result",
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    phase="commentary",
                ),
            )

        state = self._tools.get(state_key)
        if state is None:
            raise LangGraphMappingError(
                "tool_not_started",
                "tools tool_call_id",
                f"LangGraph tool call {call_id!r} mutated before tool-started",
            )
        if native_type == "tool-output-delta":
            part = DataContent(
                part_id=_part_id(state.item_id, "tool-output-deltas"),
                data=[_json_value(payload.get("delta"))],
            )
            return (
                ItemUpdated(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=state.item_id,
                        event_type="item.updated",
                        part_id=part.part_id,
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=state.item_id,
                    item_kind="tool_result",
                    op="append",
                    update=part,
                ),
            )
        if native_type == "tool-finished":
            output = payload.get("output")
            result_value = output.content if isinstance(output, ToolMessage) else output
            is_error = isinstance(output, ToolMessage) and output.status == "error"
            result = ToolResultContent(
                part_id=_part_id(state.item_id, "tool-result", call_id),
                call_id=call_id,
                result=_json_value(result_value),
                is_error=is_error,
            )
            del self._tools[state_key]
            return (
                ItemCompleted(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=state.item_id,
                        event_type="item.completed",
                        part_id=result.part_id,
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=state.item_id,
                    item_kind="tool_result",
                    snapshot=ContentSnapshot(parts=(result,)),
                ),
            )
        if native_type == "tool-error":
            del self._tools[state_key]
            message = str(payload.get("message") or "LangGraph tool call failed")
            return (
                ItemFailed(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=state.item_id,
                        event_type="item.failed",
                        part_id="tool-result",
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    item_id=state.item_id,
                    item_kind="tool_result",
                    error=ErrorInfo(
                        code="langgraph_tool_error",
                        message=message,
                        source="langgraph",
                        scope_id=scope_id,
                        item_id=state.item_id,
                        source_ref=source,
                    ),
                ),
            )
        raise LangGraphMappingError(
            "unsupported_tools_event",
            "tools event",
            f"Unsupported LangGraph tools event: {native_type}",
        )

    def _map_message_event(
        self,
        *,
        params: Mapping[str, Any],
        context: LangGraphAdapterContext,
        namespace: tuple[str, ...],
        scope_id: str,
        parent_scope_id: str | None,
        source_seq: int,
        native_event_id: str | None,
        occurrence_key: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        payload, metadata = _message_data(params.get("data"))
        node = _required_string(metadata.get("langgraph_node"), "messages metadata.langgraph_node")
        if isinstance(payload, AIMessage):
            return _map_whole_message(
                payload=payload,
                metadata=metadata,
                context=context,
                namespace=namespace,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                source_seq=source_seq,
                native_event_id=native_event_id,
                occurrence_key=occurrence_key,
                timestamp=timestamp,
                node=node,
            )

        payload = _mapping(payload, "params.data[0]")
        native_type = _required_string(payload.get("event"), "MessagesData.event")
        llm_run_id = _required_string(metadata.get("run_id"), "messages metadata.run_id")
        state_key = (scope_id, llm_run_id)

        if native_type == "message-start":
            message_id = _required_string(payload.get("id"), "message-start.id")
            if state_key in self._messages:
                raise LangGraphMappingError(
                    "message_already_started",
                    "messages metadata.run_id",
                    "LangGraph LLM run emitted a second message-start",
                )
            start_state = _MessageState(
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                llm_run_id=llm_run_id,
                message_id=message_id,
                node=node,
            )
            self._messages[state_key] = start_state
            return ()

        state = self._messages.get(state_key)
        if state is None:
            raise LangGraphMappingError(
                "message_not_started",
                "messages metadata.run_id",
                "LangGraph message mutation arrived before message-start",
            )
        if state.node != node:
            raise LangGraphMappingError(
                "conflicting_message_node",
                "messages metadata.langgraph_node",
                "LangGraph LLM run changed node during one message",
            )
        source = _message_source(
            context=context,
            namespace=namespace,
            state=state,
            source_seq=source_seq,
            native_event_id=native_event_id,
        )

        if native_type in {
            "content-block-start",
            "content-block-delta",
            "content-block-finish",
        }:
            index = _block_index(payload.get("index"))
            if native_type == "content-block-start":
                content = _mapping(payload.get("content"), "content-block-start.content")
                lane, created = _lane_for_content(state, index, content)
                lane_source = _lane_source(source, lane)
                emitted: list[RuntimeEvent] = []
                if created:
                    emitted.append(
                        _lane_started(
                            lane=lane,
                            context=context,
                            state=state,
                            occurrence_key=occurrence_key,
                            source=lane_source,
                            timestamp=timestamp,
                        )
                    )
                if lane.item_kind in {"tool_call", "tool_result"}:
                    return tuple(emitted)
                update = _text_block_snapshot(lane.item_id, index, content)
                lane.parts[index] = update
                emitted.append(
                    _lane_updated(
                        lane=lane,
                        state=state,
                        context=context,
                        update=update,
                        op="replace",
                        occurrence_key=occurrence_key,
                        source=lane_source,
                        timestamp=timestamp,
                        ordinal=index,
                    )
                )
                return tuple(emitted)

            if index in state.finished_blocks:
                raise LangGraphMappingError(
                    "content_block_already_finished",
                    f"{native_type}.index",
                    f"LangGraph content block {index} mutated after native completion",
                )
            lane = _lane_for_index(state, index)
            lane_source = _lane_source(source, lane)
            if native_type == "content-block-delta":
                delta = _mapping(payload.get("delta"), "content-block-delta.delta")
                if lane.item_kind == "tool_call":
                    _validate_tool_delta(delta)
                    return ()
                update = _text_block_delta(lane.item_id, index, delta, lane.item_kind)
                return (
                    _lane_updated(
                        lane=lane,
                        state=state,
                        context=context,
                        update=update,
                        op="append",
                        occurrence_key=occurrence_key,
                        source=lane_source,
                        timestamp=timestamp,
                        ordinal=index,
                    ),
                )

            content = _mapping(payload.get("content"), "content-block-finish.content")
            if lane.item_kind == "tool_call":
                tool_content = _tool_call_snapshot(lane.item_id, index, content)
                lane.parts[index] = tool_content
                lane.completed = True
                state.finished_blocks.add(index)
                return (
                    _lane_completed(
                        lane=lane,
                        state=state,
                        context=context,
                        occurrence_key=occurrence_key,
                        source=lane_source,
                        timestamp=timestamp,
                    ),
                )
            if lane.item_kind == "tool_result":
                result_content = _server_tool_result_snapshot(lane.item_id, index, content)
                lane.parts[index] = result_content
                lane.completed = True
                state.finished_blocks.add(index)
                return (
                    _lane_completed(
                        lane=lane,
                        state=state,
                        context=context,
                        occurrence_key=occurrence_key,
                        source=lane_source,
                        timestamp=timestamp,
                    ),
                )
            update = _text_block_snapshot(lane.item_id, index, content)
            lane.parts[index] = update
            state.finished_blocks.add(index)
            return (
                _lane_updated(
                    lane=lane,
                    state=state,
                    context=context,
                    update=update,
                    op="replace",
                    occurrence_key=occurrence_key,
                    source=lane_source,
                    timestamp=timestamp,
                    ordinal=index,
                ),
            )

        if native_type == "message-finish":
            unfinished_blocks = sorted(set(state.block_lanes).difference(state.finished_blocks))
            if unfinished_blocks:
                raise LangGraphMappingError(
                    "incomplete_content_block",
                    "content-block-finish",
                    "LangGraph message finished before native block completion: "
                    f"{unfinished_blocks}",
                )
            if not state.lanes:
                lane = _new_lane(state, "message", state.message_id)
                state.lanes["message"] = lane
                started = _lane_started(
                    lane=lane,
                    context=context,
                    state=state,
                    occurrence_key=occurrence_key,
                    source=source,
                    timestamp=timestamp,
                )
                completed = _lane_completed(
                    lane=lane,
                    context=context,
                    state=state,
                    occurrence_key=occurrence_key,
                    source=source,
                    timestamp=timestamp,
                )
                del self._messages[state_key]
                return (started, completed)

            completed_events: list[RuntimeEvent] = []
            for lane in state.lanes.values():
                if lane.completed:
                    continue
                lane.completed = True
                completed_events.append(
                    _lane_completed(
                        lane=lane,
                        context=context,
                        state=state,
                        occurrence_key=occurrence_key,
                        source=_lane_source(source, lane),
                        timestamp=timestamp,
                    )
                )
            del self._messages[state_key]
            return tuple(completed_events)

        if native_type == "error":
            del self._messages[state_key]
            raise LangGraphMappingError(
                "message_stream_error",
                "MessagesData.message",
                str(payload.get("message") or "LangGraph message stream failed"),
            )
        raise LangGraphMappingError(
            "unsupported_messages_event",
            "MessagesData.event",
            f"Unsupported LangGraph MessagesData event: {native_type}",
        )

    def _map_interrupt(
        self,
        *,
        context: LangGraphAdapterContext,
        scope_id: str,
        parent_scope_id: str | None,
        source: SourceRef,
        timestamp: float,
        occurrence_key: str,
        interrupts: Any,
    ) -> tuple[RuntimeEvent, ...]:
        reason = _interrupt_reason(interrupts)
        # Emit InteractionRequested events for each interrupt so downstream
        # consumers (e.g. agui agent) can track pending approvals before
        # RunInterrupted arrives.
        interaction_events = self._interaction_events_from_interrupts(
            context=context,
            scope_id=scope_id,
            parent_scope_id=parent_scope_id,
            source=source,
            timestamp=timestamp,
            occurrence_key=occurrence_key,
            interrupts=interrupts,
        )
        if context.checkpoint_ref is None:
            return (
                *interaction_events,
                RunInterrupted(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=context.graph_run_id,
                        event_type="run.interrupted",
                        part_id="run",
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    status="interrupted",
                    reason=reason,
                ),
            )

        checkpoint = context.checkpoint_ref
        thread_id = _required_string(checkpoint.get("thread_id"), "checkpoint.thread_id")
        checkpoint_ns = checkpoint.get("checkpoint_ns")
        if not isinstance(checkpoint_ns, str):
            raise LangGraphMappingError(
                "invalid_checkpoint_ref",
                "checkpoint.checkpoint_ns",
                "LangGraph checkpoint_ns must be a string; empty root namespace is valid",
            )
        checkpoint_id = _required_string(
            checkpoint.get("checkpoint_id"), "checkpoint.checkpoint_id"
        )
        continuation_id = stable_item_id(
            "langgraph",
            scope_id,
            "continuation",
            "graph-checkpoint",
            thread_id,
            f"checkpoint-ns:{checkpoint_ns}",
            checkpoint_id,
        )
        return (
            ContinuationCreated(
                **_envelope(
                    context=context,
                    scope_id=scope_id,
                    parent_scope_id=parent_scope_id,
                    item_id=continuation_id,
                    event_type="continuation.created",
                    part_id="checkpoint",
                    occurrence_key=occurrence_key,
                    source=source,
                    timestamp=timestamp,
                ),
                continuation_id=continuation_id,
                continuation_kind="graph_checkpoint",
                resumable=True,
                ref={
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                },
            ),
            *interaction_events,
            RunInterrupted(
                **_envelope(
                    context=context,
                    scope_id=scope_id,
                    parent_scope_id=parent_scope_id,
                    item_id=continuation_id,
                    event_type="run.interrupted",
                    part_id="run",
                    occurrence_key=occurrence_key,
                    source=source,
                    timestamp=timestamp,
                ),
                status="interrupted",
                reason=reason,
                continuation_id=continuation_id,
            ),
        )

    def _interaction_events_from_interrupts(
        self,
        *,
        context: LangGraphAdapterContext,
        scope_id: str,
        parent_scope_id: str | None,
        source: SourceRef,
        timestamp: float,
        occurrence_key: str,
        interrupts: Any,
    ) -> tuple[RuntimeEvent, ...]:
        """Emit InteractionRequested for each langgraph interrupt."""
        if not isinstance(interrupts, Sequence) or isinstance(interrupts, (str, bytes)):
            return ()
        events: list[RuntimeEvent] = []
        for idx, intr in enumerate(interrupts):
            intr_id = ""
            detail_value: Any = None
            if isinstance(intr, Mapping):
                intr_id = str(intr.get("id") or intr.get("approval_request_id") or "")
                detail_value = intr.get("value")
            else:
                intr_id = str(getattr(intr, "id", "") or "")
                detail_value = getattr(intr, "value", None)
            interaction_id = intr_id or stable_item_id(
                "langgraph", scope_id, "interaction", str(idx)
            )
            item_id = stable_item_id("langgraph", scope_id, "interaction", str(idx))
            detail_json: Any = (
                detail_value
                if isinstance(detail_value, (dict, list, str, int, float, bool, type(None)))
                else None
            )
            events.append(
                InteractionRequested(
                    **_envelope(
                        context=context,
                        scope_id=scope_id,
                        parent_scope_id=parent_scope_id,
                        item_id=item_id,
                        event_type="interaction.requested",
                        part_id="interaction",
                        occurrence_key=occurrence_key,
                        source=source,
                        timestamp=timestamp,
                    ),
                    interaction_id=interaction_id,
                    interaction_kind="approval",
                    request=ApprovalRequest(
                        call_id=intr_id or None,
                        kind="tool",
                        detail=detail_json,
                    ),
                )
            )
        return tuple(events)


def _map_data_channel(
    *,
    context: LangGraphAdapterContext,
    scope_id: str,
    parent_scope_id: str | None,
    method: str,
    source_seq: int,
    source: SourceRef,
    timestamp: float,
    occurrence_key: str,
    value: Any,
) -> tuple[RuntimeEvent, ...]:
    item_id = stable_item_id("langgraph", scope_id, "channel", method, source_seq)
    part_id = _part_id(item_id, "channel", method)
    content = DataContent(part_id=part_id, data=_json_value(value))
    return (
        ItemStarted(
            **_envelope(
                context=context,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                item_id=item_id,
                event_type="item.started",
                part_id=part_id,
                occurrence_key=occurrence_key,
                source=source,
                timestamp=timestamp,
            ),
            item_id=item_id,
            item_kind="data",
            phase="commentary",
        ),
        ItemCompleted(
            **_envelope(
                context=context,
                scope_id=scope_id,
                parent_scope_id=parent_scope_id,
                item_id=item_id,
                event_type="item.completed",
                part_id=part_id,
                occurrence_key=occurrence_key,
                source=source,
                timestamp=timestamp,
            ),
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
        raise LangGraphMappingError(
            "content_block_already_started",
            "content-block-start.index",
            f"LangGraph content block {index} started twice",
        )
    block_type = _required_string(content.get("type"), "content block type")
    if block_type == "text":
        lane_key = "message"
        item_kind: Literal["message", "reasoning", "tool_call", "tool_result"] = "message"
        native_item_id = state.message_id
    elif block_type == "reasoning":
        lane_key = "reasoning"
        item_kind = "reasoning"
        native_item_id = state.message_id
    elif block_type in {
        "tool_call",
        "tool_call_chunk",
        "server_tool_call",
        "server_tool_call_chunk",
    }:
        call_id = _required_string(content.get("id"), "tool_call.id")
        lane_key = f"tool_call:{call_id}"
        item_kind = "tool_call"
        native_item_id = call_id
    elif block_type == "server_tool_result":
        call_id = _required_string(content.get("tool_call_id"), "server_tool_result.tool_call_id")
        lane_key = f"provider_tool_result:{call_id}"
        item_kind = "tool_result"
        native_item_id = call_id
    else:
        raise LangGraphMappingError(
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
        raise LangGraphMappingError(
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
    *,
    lane: _ItemLane,
    state: _MessageState,
    context: LangGraphAdapterContext,
    occurrence_key: str,
    source: SourceRef,
    timestamp: float,
) -> ItemStarted:
    return ItemStarted(
        **_envelope(
            context=context,
            scope_id=state.scope_id,
            parent_scope_id=state.parent_scope_id,
            item_id=lane.item_id,
            event_type="item.started",
            part_id=lane.item_kind,
            occurrence_key=occurrence_key,
            source=source,
            timestamp=timestamp,
        ),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        phase=lane.phase,
    )


def _lane_updated(
    *,
    lane: _ItemLane,
    state: _MessageState,
    context: LangGraphAdapterContext,
    update: ContentValue,
    op: Literal["append", "replace"],
    occurrence_key: str,
    source: SourceRef,
    timestamp: float,
    ordinal: int,
) -> ItemUpdated:
    return ItemUpdated(
        **_envelope(
            context=context,
            scope_id=state.scope_id,
            parent_scope_id=state.parent_scope_id,
            item_id=lane.item_id,
            event_type="item.updated",
            part_id=update.part_id,
            occurrence_key=occurrence_key,
            source=source,
            timestamp=timestamp,
            ordinal=ordinal,
        ),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        op=op,
        update=update,
    )


def _lane_completed(
    *,
    lane: _ItemLane,
    state: _MessageState,
    context: LangGraphAdapterContext,
    occurrence_key: str,
    source: SourceRef,
    timestamp: float,
) -> ItemCompleted:
    return ItemCompleted(
        **_envelope(
            context=context,
            scope_id=state.scope_id,
            parent_scope_id=state.parent_scope_id,
            item_id=lane.item_id,
            event_type="item.completed",
            part_id="snapshot",
            occurrence_key=occurrence_key,
            source=source,
            timestamp=timestamp,
        ),
        item_id=lane.item_id,
        item_kind=lane.item_kind,
        snapshot=ContentSnapshot(parts=tuple(lane.parts[index] for index in sorted(lane.parts))),
    )


def _map_whole_message(
    *,
    payload: AIMessage,
    metadata: Mapping[str, Any],
    context: LangGraphAdapterContext,
    namespace: tuple[str, ...],
    scope_id: str,
    parent_scope_id: str | None,
    source_seq: int,
    native_event_id: str | None,
    occurrence_key: str,
    timestamp: float,
    node: str,
) -> tuple[RuntimeEvent, ...]:
    message_id = _required_string(payload.id, "whole message.id")
    llm_run_id = _optional_string(metadata.get("run_id")) or context.graph_run_id
    state = _MessageState(
        scope_id=scope_id,
        parent_scope_id=parent_scope_id,
        llm_run_id=llm_run_id,
        message_id=message_id,
        node=node,
    )
    source = _message_source(
        context=context,
        namespace=namespace,
        state=state,
        source_seq=source_seq,
        native_event_id=native_event_id,
    )

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
        raise LangGraphMappingError(
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
            lane_key = f"tool_call:{call_id}"
            if lane_key in state.lanes:
                continue
            lane, _ = _lane_for_content(state, offset, normalized_call)
            lane.parts[offset] = _tool_call_snapshot(lane.item_id, offset, normalized_call)

    if not state.lanes:
        state.lanes["message"] = _new_lane(state, "message", state.message_id)

    emitted: list[RuntimeEvent] = []
    for lane in state.lanes.values():
        lane_source = _lane_source(source, lane)
        emitted.append(
            _lane_started(
                lane=lane,
                state=state,
                context=context,
                occurrence_key=occurrence_key,
                source=lane_source,
                timestamp=timestamp,
            )
        )
        lane.completed = True
        emitted.append(
            _lane_completed(
                lane=lane,
                state=state,
                context=context,
                occurrence_key=occurrence_key,
                source=lane_source,
                timestamp=timestamp,
            )
        )
    return tuple(emitted)


def _message_source(
    *,
    context: LangGraphAdapterContext,
    namespace: tuple[str, ...],
    state: _MessageState,
    source_seq: int,
    native_event_id: str | None,
) -> SourceRef:
    return SourceRef(
        framework="langgraph",
        native_event_id=native_event_id,
        native_cursor=str(source_seq),
        native_run_id=state.llm_run_id,
        native_item_id=state.message_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "stream_version": "v3",
                "channel": "messages",
                "graph_run_id": context.graph_run_id,
                "namespace": list(namespace),
                "node": state.node,
                "seq_semantics": "source_cursor",
            },
        ),
    )


def _tool_source(
    *,
    context: LangGraphAdapterContext,
    namespace: tuple[str, ...],
    call_id: str,
    source_seq: int,
    native_event_id: str | None,
) -> SourceRef:
    return SourceRef(
        framework="langgraph",
        native_event_id=native_event_id,
        native_cursor=str(source_seq),
        native_run_id=context.graph_run_id,
        native_item_id=call_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "stream_version": "v3",
                "channel": "tools",
                "namespace": list(namespace),
                "seq_semantics": "source_cursor",
            },
        ),
    )


def _lifecycle_source(
    *,
    context: LangGraphAdapterContext,
    emitter_namespace: tuple[str, ...],
    target_namespace: tuple[str, ...],
    source_seq: int,
    native_event_id: str | None,
) -> SourceRef:
    return SourceRef(
        framework="langgraph",
        native_event_id=native_event_id,
        native_cursor=str(source_seq),
        native_run_id=context.graph_run_id,
        native_item_id=target_namespace[-1],
        metadata=cast(
            dict[str, JsonValue],
            {
                "stream_version": "v3",
                "channel": "lifecycle",
                "emitter_namespace": list(emitter_namespace),
                "target_namespace": list(target_namespace),
                "seq_semantics": "source_cursor",
            },
        ),
    )


def _lifecycle_progress(
    *,
    native_type: str,
    context: LangGraphAdapterContext,
    state: _LifecycleState,
    occurrence_key: str,
    source: SourceRef,
    timestamp: float,
) -> RunProgress:
    return RunProgress(
        **_envelope(
            context=context,
            scope_id=state.scope_id,
            parent_scope_id=state.parent_scope_id,
            item_id=state.item_id,
            event_type="run.progress",
            part_id=f"lifecycle:{native_type}",
            occurrence_key=occurrence_key,
            source=source,
            timestamp=timestamp,
        ),
        status="running",
        message=f"LangGraph subgraph {native_type}",
    )


def _protocol_source(
    *,
    context: LangGraphAdapterContext,
    method: str,
    namespace: tuple[str, ...],
    source_seq: int,
    native_event_id: str | None,
) -> SourceRef:
    return SourceRef(
        framework="langgraph",
        native_event_id=native_event_id,
        native_cursor=str(source_seq),
        native_run_id=context.graph_run_id,
        metadata=cast(
            dict[str, JsonValue],
            {
                "stream_version": "v3",
                "channel": method,
                "namespace": list(namespace),
                "seq_semantics": "source_cursor",
            },
        ),
    )


def _envelope(
    *,
    context: LangGraphAdapterContext,
    scope_id: str,
    parent_scope_id: str | None,
    item_id: str,
    event_type: str,
    part_id: str,
    occurrence_key: str,
    source: SourceRef,
    timestamp: float,
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


def _message_data(value: Any) -> tuple[Any, Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise LangGraphMappingError(
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
        raise LangGraphMappingError(
            "unsupported_content_block",
            "content.type",
            f"Unsupported LangGraph text-like block type: {block_type}",
        )
    # langchain-protocol 0.0.18 makes the reasoning body optional on both
    # ReasoningContentBlock shapes. Empty is therefore an authoritative native
    # snapshot; later deltas may append and finish may replace it again.
    text = content.get(field_name, "") if block_type == "reasoning" else content.get(field_name)
    if not isinstance(text, str):
        raise LangGraphMappingError(
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
        block_type = "text"
        field_name = "text"
    elif item_kind == "reasoning" and delta_type == "reasoning-delta":
        block_type = "reasoning"
        field_name = "reasoning"
    else:
        raise LangGraphMappingError(
            "unsupported_content_delta",
            "delta.type",
            f"Unsupported LangGraph content delta type: {delta_type}",
        )
    part_id = _part_id(item_id, "content-block", block_type, index)
    text = delta.get(field_name)
    if not isinstance(text, str):
        raise LangGraphMappingError(
            "invalid_content_delta",
            f"delta.{field_name}",
            f"LangGraph {delta_type} requires string {field_name}",
        )
    return TextContent(part_id=part_id, text=text)


def _validate_tool_delta(delta: Mapping[str, Any]) -> None:
    delta_type = _required_string(delta.get("type"), "content delta type")
    if delta_type == "block-delta":
        fields = _mapping(delta.get("fields"), "block-delta.fields")
        field_type = _required_string(fields.get("type"), "block-delta.fields.type")
        if field_type in {
            "tool_call",
            "tool_call_chunk",
            "server_tool_call",
            "server_tool_call_chunk",
        }:
            return
    if delta_type in {
        "tool_call",
        "tool_call_chunk",
        "tool_call-delta",
        "server_tool_call",
        "server_tool_call_chunk",
    }:
        return
    raise LangGraphMappingError(
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
    if block_type not in {
        "tool_call",
        "tool_call_chunk",
        "server_tool_call",
        "server_tool_call_chunk",
    }:
        raise LangGraphMappingError(
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
        raise LangGraphMappingError(
            "unsupported_content_block",
            "content.type",
            f"Expected LangGraph server tool result block, got: {block_type}",
        )
    call_id = _required_string(content.get("tool_call_id"), "server_tool_result.tool_call_id")
    status = _required_string(content.get("status"), "server_tool_result.status")
    if status not in {"success", "error"}:
        raise LangGraphMappingError(
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
            raise LangGraphMappingError(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol data contains a non-finite float",
            )
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise LangGraphMappingError(
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
            raise LangGraphMappingError(
                "non_json_protocol_data",
                "ProtocolEvent.params.data",
                "LangGraph protocol model returned itself from model_dump",
            )
        return _json_value(dumped)
    raise LangGraphMappingError(
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
        raise LangGraphMappingError(
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
        raise LangGraphMappingError(
            "invalid_event_shape",
            field_name,
            f"LangGraph {field_name} must be an object",
        )
    return value


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LangGraphMappingError(
            "missing_native_identity",
            field_name,
            f"LangGraph {field_name} must be a non-empty string",
        )
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _block_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LangGraphMappingError(
            "invalid_block_index",
            "index",
            "LangGraph content block index must be a non-negative integer",
        )
    return int(value)


def _source_seq(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LangGraphMappingError(
            "missing_source_cursor",
            "seq",
            "LangGraph ProtocolEvent requires its non-negative root mux seq",
        )
    return int(value)


def _protocol_timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LangGraphMappingError(
            "invalid_timestamp",
            "params.timestamp",
            "LangGraph ProtocolEvent timestamp must be epoch milliseconds",
        )
    return float(value) / 1000.0


def _interrupt_reason(interrupts: Any) -> str | None:
    if not isinstance(interrupts, Sequence) or isinstance(interrupts, (str, bytes)):
        raise LangGraphMappingError(
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
    "LangGraphEventAdapter",
    "LangGraphMappingError",
]
