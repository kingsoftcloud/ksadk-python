"""LangGraph 1.2.x raw v3 ProtocolEvents to RuntimeEvent schema v2."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, cast

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
                    code, field_name = "open_messages_at_stream_end", "messages metadata.run_id"
                elif self._tools and not self._messages and not self._lifecycles:
                    code, field_name = "open_tools_at_stream_end", "tools tool_call_id"
                elif self._lifecycles and not self._messages and not self._tools:
                    code, field_name = "open_lifecycle_at_stream_end", "lifecycle namespace"
                else:
                    code, field_name = "open_items_at_stream_end", "ProtocolEvent"
                _fail(
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
            _fail(
                "invalid_protocol_event",
                "type",
                "LangGraph ProtocolEvent.type must be 'event'",
            )
        method = _required_string(event.get("method"), "ProtocolEvent.method")
        params = _mapping(event.get("params"), "ProtocolEvent.params")
        namespace = _namespace(params.get("namespace"))
        source_seq = _source_seq(event.get("seq"))
        native_event_id = _optional_string(event.get("event_id"))
        frame = _Frame(
            namespace=namespace,
            scope_id=_scope_id(context.graph_run_id, namespace),
            parent_scope_id=_parent_scope_id(context.graph_run_id, namespace),
            source_seq=source_seq,
            native_event_id=native_event_id,
            occurrence_key=native_event_id or f"seq:{source_seq}",
            timestamp=_protocol_timestamp(params.get("timestamp")),
        )

        method_lane = {
            "messages": self._map_message_event,
            "tools": self._map_tool_event,
            "lifecycle": self._map_lifecycle_event,
        }.get(method)
        if method_lane is not None:
            return method_lane(params=params, context=context, frame=frame)

        source = _source_ref(
            channel=method,
            native_run_id=context.graph_run_id,
            native_item_id=None,
            source_seq=source_seq,
            native_event_id=native_event_id,
            extra={"namespace": list(namespace)},
        )
        if "data" not in params:
            _fail(
                "missing_protocol_data",
                "ProtocolEvent.params.data",
                f"LangGraph {method} event requires params.data",
            )
        interrupts = params.get("interrupts", ())
        if method == "values" and interrupts:
            return self._map_interrupt(
                context=context, frame=frame, source=source, interrupts=interrupts
            )
        return _map_data_channel(
            context=context, frame=frame, method=method, source=source, value=params["data"]
        )

    def _map_lifecycle_event(
        self,
        *,
        params: Mapping[str, Any],
        context: LangGraphAdapterContext,
        frame: _Frame,
    ) -> tuple[RuntimeEvent, ...]:
        env = _envelope(context, frame.occurrence_key, frame.timestamp)
        payload = _mapping(params.get("data"), "lifecycle data")
        native_type = _required_string(payload.get("event"), "lifecycle event")
        target_namespace = _namespace(payload.get("namespace"))
        if not target_namespace:
            _fail(
                "unsupported_root_lifecycle",
                "lifecycle namespace",
                "LangGraph v3 lifecycle events must identify a nested target scope",
            )
        scope_id = _scope_id(context.graph_run_id, target_namespace)
        parent_scope_id = _parent_scope_id(context.graph_run_id, target_namespace)
        item_id = stable_item_id(
            "langgraph", scope_id, "lifecycle", _namespace_identity(target_namespace)
        )
        source = _source_ref(
            channel="lifecycle",
            native_run_id=context.graph_run_id,
            native_item_id=target_namespace[-1],
            source_seq=frame.source_seq,
            native_event_id=frame.native_event_id,
            extra={
                "emitter_namespace": list(frame.namespace),
                "target_namespace": list(target_namespace),
            },
        )
        part = DataContent(
            part_id=_part_id(item_id, "lifecycle-status"),
            data=_json_value(payload),
        )
        envelope = lambda event_type: env(  # noqa: E731
            scope_id, parent_scope_id, item_id, event_type, part.part_id, source
        )

        if native_type == "started":
            if scope_id in self._lifecycles:
                _fail(
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
                RunProgress(
                    **envelope("run.progress"),
                    status="running",
                    message=f"LangGraph subgraph {native_type}",
                ),
                ItemStarted(
                    **envelope("item.started"),
                    item_id=item_id,
                    item_kind="status",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(part,)),
                ),
            )

        terminal_state = self._lifecycles.get(scope_id)
        if terminal_state is None:
            _fail(
                "lifecycle_not_started",
                "lifecycle namespace",
                "LangGraph nested lifecycle terminated before started",
            )
        if native_type == "failed":
            del self._lifecycles[scope_id]
            message = str(payload.get("error") or "LangGraph subgraph failed")
            return (
                ItemFailed(
                    **envelope("item.failed"),
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
            _fail(
                "unsupported_lifecycle_event",
                "lifecycle event",
                f"Unsupported LangGraph lifecycle event: {native_type}",
            )
        del self._lifecycles[scope_id]
        progress = (
            RunProgress(
                **envelope("run.progress"),
                status="running",
                message=f"LangGraph subgraph {native_type}",
            )
            if native_type not in _LIFECYCLE_QUIET_TYPES
            else None
        )
        completed = ItemCompleted(
            **envelope("item.completed"),
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
        frame: _Frame,
    ) -> tuple[RuntimeEvent, ...]:
        env = _envelope(context, frame.occurrence_key, frame.timestamp)
        payload = _mapping(params.get("data"), "tools data")
        native_type = _required_string(payload.get("event"), "tools event")
        call_id = _required_string(payload.get("tool_call_id"), "tools tool_call_id")
        state_key = (frame.scope_id, call_id)
        source = _source_ref(
            channel="tools",
            native_run_id=context.graph_run_id,
            native_item_id=call_id,
            source_seq=frame.source_seq,
            native_event_id=frame.native_event_id,
            extra={"namespace": list(frame.namespace)},
        )
        if native_type == "tool-started":
            if state_key in self._tools:
                _fail(
                    "tool_already_started",
                    "tools tool_call_id",
                    f"LangGraph tool call {call_id!r} started twice",
                )
            name = _required_string(payload.get("tool_name"), "tools tool_name")
            item_id = stable_item_id("langgraph", frame.scope_id, "tool_result", call_id)
            self._tools[state_key] = _ToolState(
                scope_id=frame.scope_id,
                parent_scope_id=frame.parent_scope_id,
                call_id=call_id,
                name=name,
                item_id=item_id,
            )
            return (
                ItemStarted(
                    **env(
                        frame.scope_id,
                        frame.parent_scope_id,
                        item_id,
                        "item.started",
                        "tool-result",
                        source,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    phase="commentary",
                ),
            )

        state = self._tools.get(state_key)
        if state is None:
            _fail(
                "tool_not_started",
                "tools tool_call_id",
                f"LangGraph tool call {call_id!r} mutated before tool-started",
            )
        envelope = lambda event_type, part_id: env(  # noqa: E731
            frame.scope_id, frame.parent_scope_id, state.item_id, event_type, part_id, source
        )

        if native_type == "tool-output-delta":
            part = DataContent(
                part_id=_part_id(state.item_id, "tool-output-deltas"),
                data=[_json_value(payload.get("delta"))],
            )
            return (
                ItemUpdated(
                    **envelope("item.updated", part.part_id),
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
                    **envelope("item.completed", result.part_id),
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
                    **envelope("item.failed", "tool-result"),
                    item_id=state.item_id,
                    item_kind="tool_result",
                    error=ErrorInfo(
                        code="langgraph_tool_error",
                        message=message,
                        source="langgraph",
                        scope_id=frame.scope_id,
                        item_id=state.item_id,
                        source_ref=source,
                    ),
                ),
            )
        _fail(
            "unsupported_tools_event",
            "tools event",
            f"Unsupported LangGraph tools event: {native_type}",
        )

    def _map_message_event(
        self,
        *,
        params: Mapping[str, Any],
        context: LangGraphAdapterContext,
        frame: _Frame,
    ) -> tuple[RuntimeEvent, ...]:
        payload, metadata = _message_data(params.get("data"))
        node = _required_string(metadata.get("langgraph_node"), "messages metadata.langgraph_node")
        if isinstance(payload, AIMessage):
            return _map_whole_message(
                payload=payload,
                metadata=metadata,
                context=context,
                frame=frame,
                node=node,
            )

        payload = _mapping(payload, "params.data[0]")
        native_type = _required_string(payload.get("event"), "MessagesData.event")
        llm_run_id = _required_string(metadata.get("run_id"), "messages metadata.run_id")
        state_key = (frame.scope_id, llm_run_id)

        if native_type == "message-start":
            message_id = _required_string(payload.get("id"), "message-start.id")
            if state_key in self._messages:
                _fail(
                    "message_already_started",
                    "messages metadata.run_id",
                    "LangGraph LLM run emitted a second message-start",
                )
            self._messages[state_key] = _MessageState(
                scope_id=frame.scope_id,
                parent_scope_id=frame.parent_scope_id,
                llm_run_id=llm_run_id,
                message_id=message_id,
                node=node,
            )
            return ()

        state = self._messages.get(state_key)
        if state is None:
            _fail(
                "message_not_started",
                "messages metadata.run_id",
                "LangGraph message mutation arrived before message-start",
            )
        if state.node != node:
            _fail(
                "conflicting_message_node",
                "messages metadata.langgraph_node",
                "LangGraph LLM run changed node during one message",
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
        lane_env = lambda lane, event_type, part_id, ordinal=0: env(  # noqa: E731
            state.scope_id,
            state.parent_scope_id,
            lane.item_id,
            event_type,
            part_id,
            _lane_source(source, lane),
            ordinal,
        )

        if native_type in {"content-block-start", "content-block-delta", "content-block-finish"}:
            return self._map_content_block_event(
                payload=payload,
                native_type=native_type,
                state=state,
                lane_env=lane_env,
                source=source,
            )

        if native_type == "message-finish":
            unfinished_blocks = sorted(set(state.block_lanes).difference(state.finished_blocks))
            if unfinished_blocks:
                _fail(
                    "incomplete_content_block",
                    "content-block-finish",
                    "LangGraph message finished before native block completion: "
                    f"{unfinished_blocks}",
                )
            del self._messages[state_key]
            if not state.lanes:
                lane = _new_lane(state, "message", state.message_id)
                state.lanes["message"] = lane
                return (
                    _lane_started(lane_env, lane),
                    _lane_completed(lane_env, lane),
                )
            completed_events: list[RuntimeEvent] = []
            for lane in state.lanes.values():
                if lane.completed:
                    continue
                lane.completed = True
                completed_events.append(_lane_completed(lane_env, lane))
            return tuple(completed_events)

        if native_type == "error":
            del self._messages[state_key]
            _fail(
                "message_stream_error",
                "MessagesData.message",
                str(payload.get("message") or "LangGraph message stream failed"),
            )
        _fail(
            "unsupported_messages_event",
            "MessagesData.event",
            f"Unsupported LangGraph MessagesData event: {native_type}",
        )

    def _map_content_block_event(
        self,
        *,
        payload: Mapping[str, Any],
        native_type: str,
        state: _MessageState,
        lane_env: Callable[..., dict[str, Any]],
        source: SourceRef,
    ) -> tuple[RuntimeEvent, ...]:
        index = _block_index(payload.get("index"))
        if native_type == "content-block-start":
            content = _mapping(payload.get("content"), "content-block-start.content")
            lane, created = _lane_for_content(state, index, content)
            emitted: list[RuntimeEvent] = []
            if created:
                emitted.append(_lane_started(lane_env, lane))
            if lane.item_kind in {"tool_call", "tool_result"}:
                return tuple(emitted)
            update = _text_block_snapshot(lane.item_id, index, content)
            lane.parts[index] = update
            emitted.append(_lane_updated(lane_env, lane, update, "replace", index))
            return tuple(emitted)

        if index in state.finished_blocks:
            _fail(
                "content_block_already_finished",
                f"{native_type}.index",
                f"LangGraph content block {index} mutated after native completion",
            )
        lane = _lane_for_index(state, index)
        if native_type == "content-block-delta":
            delta = _mapping(payload.get("delta"), "content-block-delta.delta")
            if lane.item_kind == "tool_call":
                _validate_tool_delta(delta)
                return ()
            update = _text_block_delta(lane.item_id, index, delta, lane.item_kind)
            return (_lane_updated(lane_env, lane, update, "append", index),)

        content = _mapping(payload.get("content"), "content-block-finish.content")
        if lane.item_kind in {"tool_call", "tool_result"}:
            if lane.item_kind == "tool_call":
                lane.parts[index] = _tool_call_snapshot(lane.item_id, index, content)
            else:
                lane.parts[index] = _server_tool_result_snapshot(lane.item_id, index, content)
            lane.completed = True
            state.finished_blocks.add(index)
            return (_lane_completed(lane_env, lane),)
        update = _text_block_snapshot(lane.item_id, index, content)
        lane.parts[index] = update
        state.finished_blocks.add(index)
        return (_lane_updated(lane_env, lane, update, "replace", index),)

    def _map_interrupt(
        self,
        *,
        context: LangGraphAdapterContext,
        frame: _Frame,
        source: SourceRef,
        interrupts: Any,
    ) -> tuple[RuntimeEvent, ...]:
        env = _envelope(context, frame.occurrence_key, frame.timestamp)
        reason = _interrupt_reason(interrupts)
        # Emit InteractionRequested events for each interrupt so downstream
        # consumers (e.g. agui agent) can track pending approvals before
        # RunInterrupted arrives.
        interaction_events = self._interaction_events_from_interrupts(
            context=context, frame=frame, source=source, env=env, interrupts=interrupts
        )
        if context.checkpoint_ref is None:
            return (
                *interaction_events,
                RunInterrupted(
                    **env(
                        frame.scope_id,
                        frame.parent_scope_id,
                        context.graph_run_id,
                        "run.interrupted",
                        "run",
                        source,
                    ),
                    status="interrupted",
                    reason=reason,
                ),
            )

        checkpoint = context.checkpoint_ref
        thread_id = _required_string(checkpoint.get("thread_id"), "checkpoint.thread_id")
        checkpoint_ns = checkpoint.get("checkpoint_ns")
        if not isinstance(checkpoint_ns, str):
            _fail(
                "invalid_checkpoint_ref",
                "checkpoint.checkpoint_ns",
                "LangGraph checkpoint_ns must be a string; empty root namespace is valid",
            )
        checkpoint_id = _required_string(
            checkpoint.get("checkpoint_id"), "checkpoint.checkpoint_id"
        )
        continuation_id = stable_item_id(
            "langgraph",
            frame.scope_id,
            "continuation",
            "graph-checkpoint",
            thread_id,
            f"checkpoint-ns:{checkpoint_ns}",
            checkpoint_id,
        )
        return (
            ContinuationCreated(
                **env(
                    frame.scope_id,
                    frame.parent_scope_id,
                    continuation_id,
                    "continuation.created",
                    "checkpoint",
                    source,
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
                **env(
                    frame.scope_id,
                    frame.parent_scope_id,
                    continuation_id,
                    "run.interrupted",
                    "run",
                    source,
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
        frame: _Frame,
        source: SourceRef,
        env: Callable[..., dict[str, Any]],
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
            item_id = stable_item_id("langgraph", frame.scope_id, "interaction", str(idx))
            interaction_id = intr_id or item_id
            detail_json: Any = (
                detail_value
                if isinstance(detail_value, (dict, list, str, int, float, bool, type(None)))
                else None
            )
            events.append(
                InteractionRequested(
                    **env(
                        frame.scope_id,
                        frame.parent_scope_id,
                        item_id,
                        "interaction.requested",
                        "interaction",
                        source,
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
    "LangGraphEventAdapter",
    "LangGraphMappingError",
]
