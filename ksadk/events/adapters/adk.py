"""Google ADK events to canonical RuntimeEvent schema v2."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from importlib.metadata import version
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
    max_native_events: int = 32768
    _next_seq: int = field(init=False, repr=False)
    _ordinals: dict[_OrdinalKey, int] = field(
        default_factory=lambda: defaultdict(int), init=False, repr=False
    )
    _open_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)
    _open_item_kinds: dict[_ItemKey, ItemKind] = field(default_factory=dict, init=False, repr=False)
    _completed_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)
    _remote_completed_text: dict[_ItemKey, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _output_refs: list[OutputRef] = field(default_factory=list, init=False, repr=False)
    _output_replacement_items: set[_ItemKey] = field(default_factory=set, init=False, repr=False)
    _active_text_items: dict[tuple[str, str], str] = field(
        default_factory=dict, init=False, repr=False
    )
    _pending_text: dict[_ItemKey, tuple[dict[str, Any], str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _partial_deliveries: int = field(default=0, init=False, repr=False)
    _seen_native_events: set[tuple[str, str, str]] = field(
        default_factory=set, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("ADK adapter run_id must not be empty")
        if self.initial_seq < 0:
            raise ValueError("ADK adapter initial_seq must be non-negative")
        if self.max_native_events <= 0:
            raise ValueError("ADK adapter max_native_events must be positive")
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
        self._pending_text.pop(key, None)
        self._completed_items.add(key)
        if output:
            if key in self._output_replacement_items:
                self._output_refs.clear()
            self._output_refs.append(OutputRef(scope_id=key[0], item_id=key[1]))
        self._output_replacement_items.discard(key)

    def mark_output_replacement(self, key: _ItemKey) -> None:
        self._output_replacement_items.add(key)

    def accept_native_event(
        self, native_run_id: str, path_key: str, native_event_id: str, *, deduplicate: bool = True
    ) -> bool:
        key = (native_run_id, path_key, native_event_id)
        if deduplicate and key in self._seen_native_events:
            return False
        if len(self._seen_native_events) + self._partial_deliveries >= self.max_native_events:
            raise ValueError("ADK native event budget exceeded")
        if deduplicate:
            self._seen_native_events.add(key)
        else:
            self._partial_deliveries += 1
        return True

    def resolve_text_item(self, scope_id: str, lane: str, candidate_item_id: str) -> str:
        """Reuse the open ADK response lane across per-chunk native Event IDs."""

        return self._active_text_items.get((scope_id, lane), candidate_item_id)

    def mark_text_started(self, key: _ItemKey, lane: str, item_kind: ItemKind) -> None:
        self.mark_started(key, item_kind)
        self._active_text_items[(key[0], lane)] = key[1]

    def mark_text_completed(self, key: _ItemKey, lane: str, *, output: bool) -> None:
        active_key = (key[0], lane)
        if self._active_text_items.get(active_key) == key[1]:
            self._active_text_items.pop(active_key, None)
        self.mark_completed(key, output=output)

    def mark_failed(self, key: _ItemKey) -> None:
        self._open_items.discard(key)
        self._open_item_kinds.pop(key, None)
        self._pending_text.pop(key, None)
        self._output_replacement_items.discard(key)
        for active_key, active_item_id in tuple(self._active_text_items.items()):
            if active_key[0] == key[0] and active_item_id == key[1]:
                self._active_text_items.pop(active_key, None)


class ADKEventAdapter:
    """Map ADK native response identities without author/text inference."""

    def __init__(self, *, partial_ids_identify_response: bool | None = None):
        if partial_ids_identify_response is None:
            release = tuple(int(part) for part in version("google-adk").split(".")[:2])
            partial_ids_identify_response = release >= (2, 6)
        self._partial_ids_identify_response = partial_ids_identify_response

    def map(self, event: Event, context: ADKAdapterContext) -> tuple[RuntimeEvent, ...]:
        if getattr(event, "_canonical", None) is not None:
            # Only our in-process event type can carry an already persisted
            # fact. Public custom_metadata cannot opt into this bypass.
            from ksadk.a2a.adk import canonical_event_from_adk

            canonical = canonical_event_from_adk(event)
            if canonical is not None:
                if canonical.run_id != context.run_id or canonical.event_type.startswith("run."):
                    raise ValueError("A2A_CANONICAL_REFERENCE_CONFLICT")
                if not context.accept_native_event(
                    canonical.run_id, canonical.scope_id, canonical.event_id
                ):
                    return ()
                context._next_seq = max(context.next_seq, canonical.seq + 1)
                return (canonical,)
        native_event_id = _required_string(getattr(event, "id", None), "event id")
        native_run_id = _required_string(getattr(event, "invocation_id", None), "invocation id")
        path = str(getattr(getattr(event, "node_info", None), "path", "") or "")
        branch = str(getattr(event, "branch", "") or "")
        path_key = path or branch or "$root"
        # ADK >= 2.6 reuses one response ID for every transient partial and
        # its aggregate. These partials are not persisted by native sessions;
        # their ordered deliveries, including equal text, must remain distinct.
        # Canonical references and A2A artifact observations keep their own IDs.
        shared_partial = (
            self._partial_ids_identify_response
            and bool(getattr(event, "partial", False))
            and not (getattr(event, "custom_metadata", None) or {}).get("ksadk_a2a_stream")
        )
        if not context.accept_native_event(
            native_run_id, path_key, native_event_id, deduplicate=not shared_partial
        ):
            return ()
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
        observations = (getattr(event, "custom_metadata", None) or {}).get("ksadk_a2a_stream") or [
            None
        ]
        for remote in observations:
            remote_item_id = None
            if remote is not None:
                remote_item_id = stable_item_id(
                    "a2a", str(remote["task_id"]), str(remote["artifact_id"])
                )
            for lane in ("reasoning", "message"):
                text = remote["text"][lane] if remote else text_by_lane[lane]
                candidate_key = (
                    scope_id,
                    stable_item_id(
                        "adk", native_run_id, path_key, remote_item_id or native_event_id, lane
                    ),
                )
                if not text and not (remote and context.is_open(candidate_key)):
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
                        partial=(not remote["snapshot"]) if remote else is_partial,
                        native_item_id=remote_item_id,
                        close_at_end=bool(remote["last_chunk"]) if remote else False,
                        replace=(not remote["append"])
                        if remote
                        else any(
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

    def finish(self, context: ADKAdapterContext) -> tuple[RuntimeEvent, ...]:
        """Close a partial reasoning lane omitted from the terminal response."""

        mapped: list[RuntimeEvent] = []
        for _key, (kwargs, text) in list(context._pending_text.items()):
            if (kwargs["lane"] == "reasoning" and not kwargs.get("native_item_id")) or kwargs.get(
                "close_at_end", False
            ):
                mapped.extend(
                    self._map_text_lane(
                        **kwargs,
                        text=text,
                        partial=False,
                        replace=False,
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
        native_item_id: str | None = None,
        close_at_end: bool = False,
    ) -> list[RuntimeEvent]:
        candidate_item_id = stable_item_id(
            "adk", native_run_id, path_key, native_item_id or native_event_id, lane
        )
        item_id = (
            candidate_item_id
            if native_item_id
            else context.resolve_text_item(scope_id, lane, candidate_item_id)
        )
        key = (scope_id, item_id)
        if context.is_completed(key):
            if native_item_id and context._remote_completed_text.get(key) != text:
                raise ValueError("A2A_TERMINAL_CONTENT_CONFLICT")
            return []
        part_id = f"{lane}.text"
        item_kind: ItemKind = "reasoning" if lane == "reasoning" else "message"
        phase: EventPhase = "commentary" if lane == "reasoning" else "final_answer"
        source = _source(
            native_event_id=native_event_id,
            native_run_id=native_run_id,
            native_item_id=native_item_id or native_event_id,
            metadata={
                **source_metadata,
                **({"ksadk_output_snapshot": True} if replace and not native_item_id else {}),
            },
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
            if native_item_id:
                context.mark_started(key, item_kind)
            else:
                context.mark_text_started(key, lane, item_kind)
        content = TextContent(part_id=part_id, text=text)
        if replace and lane == "message" and not native_item_id:
            context.mark_output_replacement(key)
        if partial:
            previous = context._pending_text.get(key, ({}, ""))[1]
            context._pending_text[key] = (
                {
                    "event": event,
                    "context": context,
                    "native_event_id": native_event_id,
                    "native_run_id": native_run_id,
                    "path_key": path_key,
                    "scope_id": scope_id,
                    "source_metadata": source_metadata,
                    "lane": lane,
                    "native_item_id": native_item_id,
                    "close_at_end": close_at_end,
                },
                ("" if replace else previous) + text,
            )
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
            if native_item_id:
                context._remote_completed_text[key] = text
            context.mark_text_completed(
                key,
                lane,
                output=lane == "message" and (bool(native_item_id) or event.is_final_response()),
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
