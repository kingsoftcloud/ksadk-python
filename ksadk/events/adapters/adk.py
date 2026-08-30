"""Google ADK 2.6.3+ events to canonical RuntimeEvent schema v2."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, TypeAlias, cast

from google.adk.events import Event
from pydantic import JsonValue

from ksadk.events.canonical import (
    EventPhase,
    ItemCompleted,
    ItemKind,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    ContentSnapshot,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id

_ItemKey: TypeAlias = tuple[str, str]
_OrdinalKey: TypeAlias = tuple[str, str, str, str]


@dataclass
class ADKAdapterContext:
    """Invocation-local allocation and lifecycle state for the ADK adapter."""

    run_id: str
    initial_seq: int = 1
    _next_seq: int = field(init=False, repr=False)
    _ordinals: dict[_OrdinalKey, int] = field(
        default_factory=lambda: defaultdict(int), init=False, repr=False
    )
    _open_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)
    _open_item_kinds: dict[_ItemKey, ItemKind] = field(default_factory=dict, init=False, repr=False)
    _completed_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)
    _output_refs: list[OutputRef] = field(default_factory=list, init=False, repr=False)
    _output_replacement_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("ADK adapter run_id must not be empty")
        if self.initial_seq < 0:
            raise ValueError("ADK adapter initial_seq must be non-negative")
        self._next_seq = self.initial_seq

    def allocate_seq(self) -> int:
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def next_ordinal(self, scope_id: str, item_id: str, event_type: str, part_id: str) -> int:
        key = (scope_id, item_id, event_type, part_id)
        ordinal = self._ordinals[key]
        self._ordinals[key] += 1
        return ordinal

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def output_refs(self) -> tuple[OutputRef, ...]:
        return tuple(ref.model_copy(deep=True) for ref in self._output_refs)

    @property
    def open_items(self) -> frozenset[_ItemKey]:
        return frozenset(self._open_items)

    @property
    def open_item_kinds(self) -> dict[_ItemKey, ItemKind]:
        return dict(self._open_item_kinds)

    def is_open(self, key: _ItemKey) -> bool:
        return key in self._open_items

    def is_completed(self, key: _ItemKey) -> bool:
        return key in self._completed_items

    def mark_started(self, key: _ItemKey, item_kind: ItemKind) -> None:
        if key in self._completed_items:
            raise ValueError(f"ADK item {key[1]!r} is already completed")
        self._open_items.add(key)
        self._open_item_kinds[key] = item_kind

    def mark_completed(self, key: _ItemKey, *, output: bool) -> None:
        self._open_items.discard(key)
        self._open_item_kinds.pop(key, None)
        self._completed_items.add(key)
        if output:
            if key in self._output_replacement_items:
                self._output_refs.clear()
            self._output_refs.append(OutputRef(scope_id=key[0], item_id=key[1]))
        self._output_replacement_items.discard(key)

    def mark_output_replacement(self, key: _ItemKey) -> None:
        self._output_replacement_items.add(key)

    def mark_failed(self, key: _ItemKey) -> None:
        self._open_items.discard(key)
        self._open_item_kinds.pop(key, None)
        self._output_replacement_items.discard(key)


class ADKEventAdapter:
    """Map ADK native response identities without author/text inference."""

    def map(self, event: Event, context: ADKAdapterContext) -> tuple[RuntimeEvent, ...]:
        native_event_id = _required_string(getattr(event, "id", None), "event id")
        native_run_id = _required_string(getattr(event, "invocation_id", None), "invocation id")
        path = str(getattr(getattr(event, "node_info", None), "path", "") or "")
        branch = str(getattr(event, "branch", "") or "")
        path_key = path or branch or "$root"
        scope_id = stable_scope_id("adk", native_run_id, path_key)
        source_metadata: dict[str, JsonValue] = {"path": path, "path_key": path_key}
        author = str(getattr(event, "author", "") or "")
        if author:
            source_metadata["author"] = author
        if branch:
            source_metadata["branch"] = branch

        parts = tuple(getattr(getattr(event, "content", None), "parts", None) or ())
        mapped: list[RuntimeEvent] = []
        text_by_lane = {
            "reasoning": "".join(
                str(part.text)
                for part in parts
                if getattr(part, "text", None) and bool(getattr(part, "thought", False))
            ),
            "message": "".join(
                str(part.text)
                for part in parts
                if getattr(part, "text", None) and not bool(getattr(part, "thought", False))
            ),
        }
        is_partial = bool(getattr(event, "partial", False))
        for lane in ("reasoning", "message"):
            text = text_by_lane[lane]
            if not text:
                continue
            mapped.extend(
                self._map_text_lane(
                    event=event,
                    context=context,
                    native_event_id=native_event_id,
                    native_run_id=native_run_id,
                    path_key=path_key,
                    scope_id=scope_id,
                    source_metadata=source_metadata,
                    lane=cast("_TextLane", lane),
                    text=text,
                    partial=is_partial,
                    replace=any(
                        getattr(part, "text", None)
                        and bool(getattr(part, "thought", False)) == (lane == "reasoning")
                        and _metadata_flag(part, "ksadk_output_snapshot")
                        for part in parts
                    ),
                )
            )

        if not is_partial:
            for part in parts:
                function_call = getattr(part, "function_call", None)
                if (
                    function_call is not None
                    and getattr(function_call, "name", None) != "adk_request_input"
                ):
                    mapped.extend(
                        self._map_tool_call(
                            event=event,
                            context=context,
                            native_event_id=native_event_id,
                            native_run_id=native_run_id,
                            path_key=path_key,
                            scope_id=scope_id,
                            source_metadata=source_metadata,
                            function_call=function_call,
                        )
                    )
                function_response = getattr(part, "function_response", None)
                if (
                    function_response is not None
                    and getattr(function_response, "name", None) != "adk_request_input"
                ):
                    mapped.extend(
                        self._map_tool_result(
                            event=event,
                            context=context,
                            native_event_id=native_event_id,
                            native_run_id=native_run_id,
                            path_key=path_key,
                            scope_id=scope_id,
                            source_metadata=source_metadata,
                            function_response=function_response,
                        )
                    )
        return tuple(mapped)

    def _map_text_lane(
        self,
        *,
        event: Event,
        context: ADKAdapterContext,
        native_event_id: str,
        native_run_id: str,
        path_key: str,
        scope_id: str,
        source_metadata: dict[str, JsonValue],
        lane: _TextLane,
        text: str,
        partial: bool,
        replace: bool,
    ) -> list[RuntimeEvent]:
        item_id = stable_item_id("adk", native_run_id, path_key, native_event_id, lane)
        key = (scope_id, item_id)
        if context.is_completed(key):
            return []
        part_id = f"{lane}.text"
        item_kind: ItemKind = "reasoning" if lane == "reasoning" else "message"
        phase: EventPhase = "commentary" if lane == "reasoning" else "final_answer"
        source = _source(
            native_event_id=native_event_id,
            native_run_id=native_run_id,
            native_item_id=native_event_id,
            metadata=source_metadata,
        )
        mapped: list[RuntimeEvent] = []
        if not context.is_open(key):
            mapped.append(
                ItemStarted(
                    **_envelope(
                        event=event,
                        context=context,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part_id,
                        native_event_id=native_event_id,
                        source=source,
                    ),
                    item_id=item_id,
                    item_kind=item_kind,
                    phase=phase,
                )
            )
            context.mark_started(key, item_kind)
        content = TextContent(part_id=part_id, text=text)
        if replace and lane == "message":
            context.mark_output_replacement(key)
        if partial:
            mapped.append(
                ItemUpdated(
                    **_envelope(
                        event=event,
                        context=context,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.updated",
                        part_id=part_id,
                        native_event_id=native_event_id,
                        source=source,
                    ),
                    item_id=item_id,
                    item_kind=item_kind,
                    op="replace" if replace else "append",
                    update=content,
                )
            )
        else:
            mapped.append(
                ItemCompleted(
                    **_envelope(
                        event=event,
                        context=context,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id=part_id,
                        native_event_id=native_event_id,
                        source=source,
                    ),
                    item_id=item_id,
                    item_kind=item_kind,
                    snapshot=ContentSnapshot(parts=(content,)),
                )
            )
            context.mark_completed(
                key,
                output=lane == "message" and event.is_final_response(),
            )
        return mapped

    def _map_tool_call(
        self,
        *,
        event: Event,
        context: ADKAdapterContext,
        native_event_id: str,
        native_run_id: str,
        path_key: str,
        scope_id: str,
        source_metadata: dict[str, JsonValue],
        function_call: Any,
    ) -> list[RuntimeEvent]:
        call_id = _required_string(getattr(function_call, "id", None), "call id")
        name = _required_string(getattr(function_call, "name", None), "tool name")
        content = ToolCallContent(
            part_id="tool_call",
            call_id=call_id,
            name=name,
            arguments=cast(JsonValue, getattr(function_call, "args", None) or {}),
        )
        return self._complete_tool_item(
            event=event,
            context=context,
            native_event_id=native_event_id,
            native_run_id=native_run_id,
            path_key=path_key,
            scope_id=scope_id,
            source_metadata={**source_metadata, "tool_name": name},
            call_id=call_id,
            item_kind="tool_call",
            content=content,
        )

    def _map_tool_result(
        self,
        *,
        event: Event,
        context: ADKAdapterContext,
        native_event_id: str,
        native_run_id: str,
        path_key: str,
        scope_id: str,
        source_metadata: dict[str, JsonValue],
        function_response: Any,
    ) -> list[RuntimeEvent]:
        call_id = _required_string(getattr(function_response, "id", None), "call id")
        name = _required_string(getattr(function_response, "name", None), "tool name")
        result = cast(JsonValue, getattr(function_response, "response", None) or {})
        content = ToolResultContent(
            part_id="tool_result",
            call_id=call_id,
            result=result,
            is_error=isinstance(result, dict) and "error" in result,
        )
        return self._complete_tool_item(
            event=event,
            context=context,
            native_event_id=native_event_id,
            native_run_id=native_run_id,
            path_key=path_key,
            scope_id=scope_id,
            source_metadata={**source_metadata, "tool_name": name},
            call_id=call_id,
            item_kind="tool_result",
            content=content,
        )

    def _complete_tool_item(
        self,
        *,
        event: Event,
        context: ADKAdapterContext,
        native_event_id: str,
        native_run_id: str,
        path_key: str,
        scope_id: str,
        source_metadata: dict[str, JsonValue],
        call_id: str,
        item_kind: ItemKind,
        content: ToolCallContent | ToolResultContent,
    ) -> list[RuntimeEvent]:
        item_id = stable_item_id(
            "adk", native_run_id, path_key, native_event_id, call_id, item_kind
        )
        key = (scope_id, item_id)
        if context.is_completed(key):
            return []
        source = _source(
            native_event_id=native_event_id,
            native_run_id=native_run_id,
            native_item_id=call_id,
            metadata=source_metadata,
        )
        part_id = content.part_id
        started = ItemStarted(
            **_envelope(
                event=event,
                context=context,
                scope_id=scope_id,
                item_id=item_id,
                event_type="item.started",
                part_id=part_id,
                native_event_id=native_event_id,
                source=source,
            ),
            item_id=item_id,
            item_kind=item_kind,
            phase="commentary",
        )
        context.mark_started(key, item_kind)
        completed = ItemCompleted(
            **_envelope(
                event=event,
                context=context,
                scope_id=scope_id,
                item_id=item_id,
                event_type="item.completed",
                part_id=part_id,
                native_event_id=native_event_id,
                source=source,
            ),
            item_id=item_id,
            item_kind=item_kind,
            snapshot=ContentSnapshot(parts=(content,)),
        )
        context.mark_completed(key, output=False)
        return [started, completed]


_TextLane: TypeAlias = str


def _required_string(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"ADK {label} must not be empty")
    return normalized


def _source(
    *,
    native_event_id: str,
    native_run_id: str,
    native_item_id: str,
    metadata: dict[str, JsonValue],
) -> SourceRef:
    return SourceRef(
        framework="adk",
        native_event_id=native_event_id,
        native_run_id=native_run_id,
        native_item_id=native_item_id,
        metadata=dict(metadata),
    )


def _envelope(
    *,
    event: Event,
    context: ADKAdapterContext,
    scope_id: str,
    item_id: str,
    event_type: str,
    part_id: str,
    native_event_id: str,
    source: SourceRef,
) -> dict[str, Any]:
    ordinal = context.next_ordinal(scope_id, item_id, event_type, part_id)
    return {
        "schema_version": 2,
        "event_id": stable_event_id(
            "adk",
            scope_id,
            item_id,
            event_type,
            part_id,
            native_event_id,
            ordinal,
        ),
        "seq": context.allocate_seq(),
        "timestamp": float(getattr(event, "timestamp", 0.0) or 0.0),
        "run_id": context.run_id,
        "scope_id": scope_id,
        "source": source,
    }


def _metadata_flag(part: Any, key: str) -> bool:
    metadata = getattr(part, "part_metadata", None)
    if isinstance(metadata, dict):
        return bool(metadata.get(key))
    getter = getattr(metadata, "get", None)
    if callable(getter):
        try:
            return bool(getter(key))
        except (KeyError, TypeError, ValueError):
            return False
    return False


__all__ = ["ADKAdapterContext", "ADKEventAdapter"]
