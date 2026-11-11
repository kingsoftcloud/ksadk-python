"""LangGraph 1.2.x raw v3 ProtocolEvents to RuntimeEvent schema v2."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.stream import AsyncGraphRunStream

from ksadk.events.adapters._langgraph_custom import CustomOutputMapper
from ksadk.events.adapters._langgraph_model_tasks import NativeModelTasks
from ksadk.events.adapters._langgraph_support import (
    _LIFECYCLE_QUIET_TYPES,
    LangGraphAdapterContext,
    LangGraphMappingError,
    _block_index,
    _envelope,
    _fail,
    _Frame,
    _interrupt_reason,
    _json_value,
    _lane_completed,
    _lane_for_content,
    _lane_for_index,
    _lane_source,
    _lane_started,
    _lane_updated,
    _LifecycleState,
    _map_data_channel,
    _map_whole_message,
    _mapping,
    _message_data,
    _message_usage_events,
    _message_visibility,
    _MessageState,
    _namespace,
    _namespace_identity,
    _new_lane,
    _optional_string,
    _parent_scope_id,
    _part_id,
    _protocol_timestamp,
    _required_string,
    _scope_id,
    _server_tool_result_snapshot,
    _source_ref,
    _source_seq,
    _text_block_delta,
    _text_block_snapshot,
    _tool_call_snapshot,
    _ToolState,
    _validate_tool_delta,
)
from ksadk.events.adapters._langgraph_tool_outcomes import NativeToolOutcomes
from ksadk.events.adapters._langgraph_usage_origin import (
    NativeUsageOrigin,
    message_usage_origin,
)
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
    StructuredInputRequest,
    UsageReported,
)
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    ToolResultContent,
)
from ksadk.events.identity import (
    stable_item_id,
)

# Native content-block types that carry a tool call identity.
_TOOL_CALL_BLOCKS = frozenset(
    "tool_call tool_call_chunk server_tool_call server_tool_call_chunk".split()
)
# Native tool-call delta shapes accepted without a payload translation.
_TOOL_DELTA_TYPES = frozenset(
    "tool_call tool_call_chunk tool_call-delta server_tool_call server_tool_call_chunk".split()
)


class LangGraphEventAdapter:
    """Consume the lossless raw log exposed by ``AsyncGraphRunStream``."""

    def __init__(
        self,
        *,
        tool_outcomes: NativeToolOutcomes | None = None,
        model_tasks: NativeModelTasks | None = None,
        usage_origin: NativeUsageOrigin | None = None,
        prior_usage_origins: set[str] | None = None,
    ) -> None:
        self._messages: dict[tuple[str, str], _MessageState] = {}
        self._tools: dict[tuple[str, str], _ToolState] = {}
        self._tool_occurrences: dict[tuple[str, str], int] = {}
        self._lifecycles: dict[str, _LifecycleState] = {}
        self._tool_outcomes = tool_outcomes
        self._model_tasks = model_tasks
        self._usage_origin = usage_origin
        self._reported_usage_runs = set(prior_usage_origins or ())
        self._custom = CustomOutputMapper()

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
            for canonical in self._custom.finish(context):
                yield canonical
            for canonical in self._close_canceled_messages(context):
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

    def _close_canceled_messages(self, context: LangGraphAdapterContext) -> Iterator[ItemFailed]:
        if self._model_tasks is None:
            return
        # Only reconcile at clean EOF. A failed graph still propagates its
        # original error, and unexplained missing terminal frames fail below.
        for key, state in tuple(self._messages.items()):
            if not self._model_tasks.was_canceled(state.llm_run_id):
                continue
            del self._messages[key]
            if state.source is None:
                continue  # no canonical lane was started
            source = state.source.model_copy(update={
                "native_event_id": None,
                "native_cursor": None,
                "metadata": {
                    key: value for key, value in state.source.metadata.items()
                    if key != "seq_semantics"
                },
            })
            env = _envelope(context, f"canceled-model:{state.llm_run_id}", time.time())
            for lane in state.lanes.values():
                if lane.completed:
                    continue
                lane_source = _lane_source(source, lane)
                yield ItemFailed(
                    **env(
                        state.scope_id, state.parent_scope_id, lane.item_id,
                        "item.failed", lane.item_kind, lane_source,
                    ),
                    item_id=lane.item_id,
                    item_kind=lane.item_kind,
                    error=ErrorInfo(
                        code="langgraph_model_stream_canceled",
                        message="LangGraph model stream was canceled before completion",
                        source="langgraph",
                        scope_id=state.scope_id,
                        item_id=lane.item_id,
                        source_ref=lane_source,
                    ),
                )

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
            # Close a writer text segment before model messages so prefix /
            # model answer / writer suffix keep their actual arrival order.
            prefix = (self._custom.finish(context)
                      if method == "messages" else ())
            return (*prefix, *method_lane(params=params, context=context, frame=frame))

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
            prefix = self._custom.finish(context)
            return (*prefix, *self._map_interrupt(
                context=context, frame=frame, source=source, interrupts=interrupts
            ))
        if method == "updates" and isinstance(params["data"], Mapping):
            if "__interrupt__" in params["data"]:
                # v3 publishes this notification again in values.interrupts.
                # Native Interrupt objects are not graph data or answer writes.
                return ()
        if method == "custom":
            output = self._custom.map(params["data"], frame=frame, source=source, context=context)
            if output is not None:
                return output
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
            occurrence = self._tool_occurrences.get(state_key, 0)
            self._tool_occurrences[state_key] = occurrence + 1
            item_id = (
                stable_item_id("langgraph", frame.scope_id, "tool_result", call_id)
                if occurrence == 0
                else stable_item_id(
                    "langgraph",
                    frame.scope_id,
                    "tool_result",
                    call_id,
                    "occurrence",
                    frame.occurrence_key,
                )
            )
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
            if self._tool_outcomes is not None:
                self._tool_outcomes.consume(frame.namespace, call_id, native_type)
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
            if self._tool_outcomes is not None and self._tool_outcomes.consume(
                frame.namespace, call_id, native_type
            ):
                # Close only this materialized execution shell. There is no
                # business result until native resume actually finishes it.
                paused_source = source.model_copy(update={"metadata": {
                    **source.metadata, "conversation_visibility": "internal",
                    "ksadk_control_flow": "interrupt",
                }})
                return (
                    ItemCompleted(
                        **env(frame.scope_id, frame.parent_scope_id, state.item_id,
                              "item.completed", "tool-interrupt", paused_source),
                        item_id=state.item_id,
                        item_kind="tool_result",
                        snapshot=ContentSnapshot(parts=()),
                    ),
                )
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
        if isinstance(payload, ToolMessage):
            # LangGraph 1.2.0 also echoes tool outcomes on the messages lane.
            # The tools lane owns their canonical result and error identity.
            return ()
        native_namespace = metadata.get("langgraph_checkpoint_ns")
        inherited_child = bool(frame.namespace) or (
            isinstance(native_namespace, str) and bool(native_namespace)
        )
        if inherited_child and isinstance(payload, (HumanMessage, SystemMessage)):
            # Native v3 child graphs echo inherited input/history messages on
            # this lane after resume. The admitted parent turn already owns
            # them; they are neither a new assistant answer nor a new turn.
            # LangGraph can emit the echo at the root lane, retaining only its
            # public checkpoint namespace metadata. Root/unknown shapes stay
            # strict when neither namespace identifies a child.
            return ()
        if isinstance(payload, AIMessage):
            events = _map_whole_message(
                payload=payload,
                metadata=metadata,
                context=context,
                frame=frame,
                node=node,
            )
            origin = message_usage_origin(payload)
            return self._usage_once(events, origin, origin=origin)

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
                visibility=_message_visibility(metadata),
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
        # Later provider frames can omit call-scoped hints. Retain the latest
        # explicit visibility for this model run, as the legacy runner did.
        visibility = _message_visibility(metadata)
        if "conversation_visibility" in state.visibility and metadata.get(
            "conversation_visibility"
        ) not in ("public", "internal", "hidden"):
            visibility.pop("conversation_visibility", None)
        state.visibility.update(visibility)
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
                **state.visibility,
            },
        )
        state.source = source
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
            completed_events: list[RuntimeEvent] = []
            if not state.lanes:
                lane = _new_lane(state, "message", state.message_id)
                state.lanes["message"] = lane
                completed_events.append(_lane_started(lane_env, lane))
            for lane in state.lanes.values():
                if lane.completed:
                    continue
                lane.completed = True
                completed_events.append(_lane_completed(lane_env, lane))
            completed_events.extend(_message_usage_events(state, env, source, payload.get("usage")))
            origin = self._usage_origin.origin_for_run(llm_run_id) if self._usage_origin else None
            return self._usage_once(tuple(completed_events), origin or llm_run_id, origin=origin)

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

    def _usage_once(
        self, events: tuple[RuntimeEvent, ...], identity: str | None, *, origin: str | None
    ) -> tuple[RuntimeEvent, ...]:
        if identity in self._reported_usage_runs:
            return tuple(event for event in events if not isinstance(event, UsageReported))
        result: list[RuntimeEvent] = []
        for event in events:
            if isinstance(event, UsageReported):
                if identity is not None:
                    self._reported_usage_runs.add(identity)
                if origin is not None:
                    event = event.model_copy(update={
                        "source": event.source.model_copy(update={
                            "metadata": {**event.source.metadata, "usage_origin": origin},
                        }),
                    })
            result.append(event)
        return tuple(result)

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
                started = _lane_started(lane_env, lane)
                if lane.item_kind == "tool_call" and state.tool_start_names.get(index):
                    # The name is already public. Arguments are authoritative
                    # only at block finish and must not be executed from here.
                    started = started.model_copy(update={
                        "source": started.source.model_copy(update={"metadata": {
                            **started.source.metadata, "tool_name": state.tool_start_names[index],
                        }}),
                    })
                emitted.append(started)
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
                # Some providers send identity only in the start frame. Reuse
                # that native block's identity, never another concurrent call's.
                fields = dict(content)
                if fields.get("id") in (None, ""):
                    fields["id"] = lane.native_item_id
                elif fields["id"] != lane.native_item_id:
                    _fail("conflicting_tool_identity", "tool_call.id", "Tool call ID changed within a block")
                if fields.get("name") in (None, ""):
                    fields["name"] = state.tool_start_names.get(index)
                lane.parts[index] = _tool_call_snapshot(lane.item_id, index, fields)
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
        # Associate the companion pause with the pending interaction so Kernel
        # preserves WAITING instead of publishing a terminal interruption.
        interaction_id = interaction_events[0].interaction_id if interaction_events else None
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
                    interaction_id=interaction_id,
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
                interaction_id=interaction_id,
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
            from ksadk.interaction.langgraph_interrupt import normalize_interrupt

            normalized = normalize_interrupt(intr)
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
            is_input = normalized.kind == "structured_input"
            request = (
                StructuredInputRequest(prompt=normalized.prompt, schema=normalized.schema)
                if is_input
                else ApprovalRequest(call_id=intr_id or None, kind="tool", detail=detail_json)
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
                    interaction_kind="structured_input" if is_input else "approval",
                    request=request,
                )
            )
        return tuple(events)


__all__ = [
    "LangGraphAdapterContext",
    "LangGraphEventAdapter",
    "LangGraphMappingError",
]
