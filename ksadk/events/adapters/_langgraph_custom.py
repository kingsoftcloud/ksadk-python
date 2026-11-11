"""Project the established get_stream_writer text/tool envelopes onto v2 items."""

from collections.abc import Mapping
from dataclasses import dataclass

from ksadk.events.adapters._langgraph_support import (
    LangGraphAdapterContext,
    _envelope,
    _Frame,
    _json_value,
)
from ksadk.events.canonical import ItemCompleted, ItemStarted, ItemUpdated, SourceRef
from ksadk.events.content import ContentSnapshot, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_item_id


@dataclass
class _TextStream:
    item_id: str
    item_kind: str
    text: str
    frame: _Frame
    source: SourceRef


class CustomOutputMapper:
    def __init__(self):
        self._text: dict[tuple[str, str], _TextStream] = {}

    def map(self, value, *, frame, source, context):
        """Only the legacy default custom lane carries these output envelopes.

        Unknown payloads and named custom channels remain lossless data. A
        business JSON string is ordinary text; no content heuristics are used.
        """
        chunk = {"type": "text", "delta": value} if isinstance(value, str) else value
        if not isinstance(chunk, Mapping):
            return None
        kind = chunk.get("type", "text")
        if not isinstance(kind, str):
            return None
        if kind in {"tool_call", "tool_result"}:
            return self._tool(chunk, frame=frame, source=source, context=context)
        if kind not in {"text", "text_delta", "thinking", "reasoning", "reasoning_delta"}:
            return None
        text = next(
            (
                chunk[key]
                for key in ("delta", "text", "content", "output", "data")
                if isinstance(chunk.get(key), str) and chunk[key]
            ),
            "",
        )
        if not text:
            return None
        item_kind = (
            "reasoning" if kind in {"thinking", "reasoning", "reasoning_delta"} else "message"
        )
        # Each item occupies its first position in conversation projections.
        # Reusing A's item after B wrote text would render A1/A2/B1, even though
        # the native stream arrived as A1/B1/A2. Start a new segment on a scope
        # switch rather than appending into an earlier conversation position.
        events = (
            list(self.finish(context))
            if any(scope != frame.scope_id for scope, _ in self._text)
            else []
        )
        key = frame.scope_id, item_kind
        current = self._text.get(key)
        if current is None:
            current = _TextStream(
                stable_item_id(
                    "langgraph",
                    frame.scope_id,
                    "custom",
                    item_kind,
                    frame.occurrence_key,
                    context.publication_epoch or "initial",
                ),
                item_kind,
                "",
                frame,
                source,
            )
            self._text[key] = current
            events.append(
                ItemStarted(
                    **self._envelope(current, context, "item.started"),
                    item_id=current.item_id,
                    item_kind=item_kind,
                    phase="final_answer" if item_kind == "message" else "commentary",
                )
            )
        current.frame, current.source = frame, source
        replace = bool(chunk.get("replace"))
        current.text = text if replace else current.text + text
        events.append(
            ItemUpdated(
                **self._envelope(current, context, "item.updated"),
                item_id=current.item_id,
                item_kind=item_kind,
                op="replace" if replace else "append",
                update=TextContent(part_id="text", text=text),
            )
        )
        return tuple(events)

    @staticmethod
    def _envelope(current, context, event_type):
        frame = current.frame
        return _envelope(context, frame.occurrence_key, frame.timestamp)(
            frame.scope_id,
            frame.parent_scope_id,
            current.item_id,
            event_type,
            "text",
            current.source,
        )

    def finish(self, context: LangGraphAdapterContext, *, scope_id=None):
        events = []
        for key, current in tuple(self._text.items()):
            if scope_id is not None and key[0] != scope_id:
                continue
            events.append(
                ItemCompleted(
                    **self._envelope(current, context, "item.completed"),
                    item_id=current.item_id,
                    item_kind=current.item_kind,
                    snapshot=ContentSnapshot(
                        parts=(TextContent(part_id="text", text=current.text),)
                    ),
                )
            )
            del self._text[key]
        return tuple(events)

    @staticmethod
    def _tool(chunk, *, frame, source, context):
        kind = chunk["type"]
        name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
        call_id = str(
            chunk.get("tool_call_id")
            or chunk.get("call_id")
            or chunk.get("run_id")
            or chunk.get("id")
            or name
        )
        item_id = stable_item_id(
            "langgraph",
            frame.scope_id,
            "custom",
            kind,
            frame.occurrence_key,
            context.publication_epoch or "initial",
        )
        part = (
            ToolCallContent(
                part_id="tool",
                call_id=call_id,
                name=name,
                arguments=_json_value(chunk.get("tool_args", chunk.get("args")) or {}),
            )
            if kind == "tool_call"
            else ToolResultContent(
                part_id="tool",
                call_id=call_id,
                result=_json_value(chunk.get("tool_output", chunk.get("output"))),
                is_error=bool(chunk.get("error")),
            )
        )
        source = source.model_copy(
            update={"native_item_id": call_id, "metadata": {**source.metadata, "tool_name": name}}
        )
        snapshot = ContentSnapshot(parts=(part,))
        env = _envelope(context, frame.occurrence_key, frame.timestamp)
        return (
            ItemStarted(
                **env(
                    frame.scope_id, frame.parent_scope_id, item_id, "item.started", "tool", source
                ),
                item_id=item_id,
                item_kind=kind,
                phase="commentary",
                initial=snapshot,
            ),
            ItemCompleted(
                **env(
                    frame.scope_id, frame.parent_scope_id, item_id, "item.completed", "tool", source
                ),
                item_id=item_id,
                item_kind=kind,
                snapshot=snapshot,
            ),
        )
