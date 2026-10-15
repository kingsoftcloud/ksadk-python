"""Request-local Responses content facts, before ordered flat stream projection.

Part identity and closure are validated here; no runtime, approval or Store
authority is introduced. Unindexed legacy deltas retain their old convention.
"""

from dataclasses import dataclass, field

from ksadk.studio.model_wire_identity import invalid_model_frame
from ksadk.studio.model_wire_tools import (
    ResponsesItems,
    _identity,
    _index,
    _object,
    _text,
)


@dataclass
class ContentPart:
    kind: str
    text: str = ""
    closed: bool = False

    def snapshot(self, text: str, *, closed: bool) -> None:
        if (self.closed and text != self.text) or not text.startswith(self.text):
            raise invalid_model_frame()
        self.text = text
        self.closed = self.closed or closed


@dataclass
class ContentItem:
    kind: str
    parts: dict[tuple[str, int], ContentPart] = field(default_factory=dict)
    closed: bool = False


_TEXT_KINDS = {"output_text", "text"}
_DELTA_KINDS = {
    "response.output_text": ("content", "output_text", "content_index"),
    "response.refusal": ("content", "refusal", "content_index"),
    "response.reasoning_summary_text": ("summary", "summary_text", "summary_index"),
    "response.reasoning_text": ("content", "reasoning_text", "content_index"),
}


@dataclass
class ResponsesContent:
    identities: ResponsesItems
    items: dict[str, ContentItem] = field(default_factory=dict)
    emitted_text: str = ""
    emitted_reasoning: str = ""
    legacy_text: str = ""
    legacy_reasoning: str = ""
    legacy_closed: set[str] = field(default_factory=set)
    refused: bool = False

    def _item(self, event: dict, *, kind: str, item: dict | None = None) -> ContentItem:
        key = self.identities.pin(event, item, kind=kind)
        return self.items.setdefault(key, ContentItem(kind))

    def _part(self, item: ContentItem, group: str, index: int, kind: str) -> ContentPart:
        previous = item.parts.get((group, index))
        if previous is not None:
            if previous.kind != kind:
                raise invalid_model_frame()
            return previous
        if item.closed:
            raise invalid_model_frame()
        part = ContentPart(kind)
        item.parts[group, index] = part
        return part

    def _part_event(self, event: dict, group: str, kind: str, index_field: str,
                    text: str, *, closed: bool, delta: bool = False) -> None:
        index = _index(event, index_field, required=True)
        _index(event, "output_index", required=True)
        item = self._item(event, kind="reasoning" if group == "summary" or kind == "reasoning_text"
                          else "message")
        if delta and item.closed:
            raise invalid_model_frame()
        part = self._part(item, group, index, kind)
        if delta:
            if part.closed:
                raise invalid_model_frame()
            part.text += text
        else:
            part.snapshot(text, closed=closed)
        self.refused = self.refused or kind == "refusal"

    def _item_snapshot(self, event: dict, *, closed: bool) -> None:
        raw = _object(event.get("item"))
        kind = _identity(raw.get("type"))
        if kind == "message" and raw.get("role") not in (None, "assistant"):
            raise invalid_model_frame()
        item = self._item(event, item=raw, kind=kind)
        if not closed and item.closed:
            raise invalid_model_frame()
        groups = (("content",) if kind == "message" else
                  ("summary", "content") if kind == "reasoning" else ())
        seen = set()
        for group in groups:
            values = raw.get(group, [])
            if not isinstance(values, list):
                raise invalid_model_frame()
            for index, value in enumerate(values):
                value = _object(value)
                part_kind = _identity(value.get("type"))
                if kind == "message" and part_kind not in _TEXT_KINDS | {"refusal"}:
                    raise invalid_model_frame()
                if kind == "reasoning" and part_kind not in (
                    {"summary_text", "text"} if group == "summary" else {"reasoning_text", "text"}
                ):
                    raise invalid_model_frame()
                # Legacy alias 'text' shares the canonical part type.
                if part_kind == "text":
                    part_kind = "output_text" if kind == "message" else (
                        "summary_text" if group == "summary" else "reasoning_text"
                    )
                text = _text(value.get("refusal" if part_kind == "refusal" else "text"))
                self._part(item, group, index, part_kind).snapshot(text, closed=closed)
                self.refused = self.refused or part_kind == "refusal"
                seen.add((group, index))
        if closed:
            if item.parts.keys() != seen:
                raise invalid_model_frame()
            item.closed = True

    def _prefix(self, *, reasoning: bool) -> str:
        pieces = []
        expected = 0
        for index, key in sorted(self.identities.indexes.items()):
            if index != expected:
                break
            expected += 1
            kind = self.identities.kinds.get(key)
            wanted = "reasoning" if reasoning else "message"
            if kind != wanted:
                continue
            item = self.items.get(key)
            if item is None or reasoning and not item.closed:
                break
            groups = ("summary", "content") if reasoning else ("content",)
            for group in groups:
                part_index = 0
                parts = sorted((i, p) for (g, i), p in item.parts.items() if g == group)
                for i, part in parts:
                    if i != part_index:
                        return "".join(pieces)
                    part_index += 1
                    if part.kind != "refusal":
                        pieces.append(part.text)
                    if not part.closed:
                        return "".join(pieces)
            if not item.closed:
                break
        return "".join(pieces)

    def public_summary(self) -> str:
        """Only indexed, validated summary parts; never reasoning content."""
        pieces = []
        expected = 0
        for index, key in sorted(self.identities.indexes.items()):
            if index != expected:
                break
            expected += 1
            if self.identities.kinds.get(key) != "reasoning":
                continue
            item = self.items.get(key)
            if item is None:
                break
            for position, (part_index, part) in enumerate(sorted(
                (i, p) for (group, i), p in item.parts.items() if group == "summary"
            )):
                if position != part_index:
                    return "".join(pieces)
                pieces.append(part.text)
                if not part.closed:
                    return "".join(pieces)
            if not item.closed:
                break
        return "".join(pieces)

    def _drain(self) -> tuple[str, str]:
        text, reasoning = self._prefix(reasoning=False), self._prefix(reasoning=True)
        if (text and self.legacy_text) or (reasoning and self.legacy_reasoning):
            raise invalid_model_frame()  # Unidentified text cannot be assigned to an indexed part.
        if (not text.startswith(self.emitted_text)
                or not reasoning.startswith(self.emitted_reasoning)):
            raise invalid_model_frame()
        tails = text[len(self.emitted_text):], reasoning[len(self.emitted_reasoning):]
        self.emitted_text, self.emitted_reasoning = text, reasoning
        return tails

    def accept(self, event: dict) -> tuple[str, str]:
        event_type = event["type"]
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            self._item_snapshot(event, closed=event_type.endswith(".done"))
        elif event_type in {"response.content_part.added", "response.content_part.done",
                            "response.reasoning_summary_part.added",
                            "response.reasoning_summary_part.done"}:
            part = _object(event.get("part"))
            kind = _identity(part.get("type"))
            summary = "reasoning_summary_part" in event_type
            allowed = {"summary_text"} if summary else {"output_text", "refusal", "reasoning_text"}
            if kind not in allowed:
                raise invalid_model_frame()
            self._part_event(event, "summary" if summary else "content", kind,
                             "summary_index" if summary else "content_index",
                             _text(part.get("refusal" if kind == "refusal" else "text")),
                             closed=event_type.endswith(".done"))
        else:
            stem, _, suffix = event_type.rpartition(".")
            spec = _DELTA_KINDS.get(stem)
            if spec is None or suffix not in {"delta", "done"}:
                return self._drain()
            group, kind, index_field = spec
            text = _text(event.get("delta" if suffix == "delta" else
                                   "refusal" if kind == "refusal" else "text"))
            if not any(field in event for field in ("item_id", "output_index", index_field)):
                # Deliberate compatibility for old proxies, not modern part validation.
                self.refused = self.refused or kind == "refusal"
                if kind == "refusal":
                    return "", ""
                attr = "legacy_text" if kind == "output_text" else "legacy_reasoning"
                previous = getattr(self, attr)
                if suffix == "done":
                    if (not text.startswith(previous)
                            or attr in self.legacy_closed and text != previous):
                        raise invalid_model_frame()
                    tail = text[len(previous):]
                    setattr(self, attr, text)
                    self.legacy_closed.add(attr)
                else:
                    if attr in self.legacy_closed:
                        raise invalid_model_frame()
                    tail = text
                    setattr(self, attr, previous + text)
                indexed = self.emitted_text if attr == "legacy_text" else self.emitted_reasoning
                if indexed:
                    raise invalid_model_frame()
                return (tail, "") if attr == "legacy_text" else ("", tail)
            self._part_event(event, group, kind, index_field, text,
                             closed=suffix == "done", delta=suffix == "delta")
        return self._drain()

    def reconcile_output(self, output: list[dict]) -> tuple[str, str]:
        seen = set()
        for index, item in enumerate(output):
            if "id" not in item and index not in self.identities.indexes:
                continue  # Legacy snapshot lacking identity; flat reconciliation still applies.
            key = _identity(item.get("id"))
            if key in seen:
                raise invalid_model_frame()
            seen.add(key)
            kind = item.get("type")
            if key not in self.items and (
                kind == "message" and self.legacy_text
                or kind == "reasoning" and self.legacy_reasoning
            ):
                self.identities.pin({"output_index": index}, item)
                continue
            self._item_snapshot({"output_index": index, "item": item}, closed=True)
        if not self.items.keys() <= seen:
            raise invalid_model_frame()
        return self._drain()

    def finish(self, *, require_closed: bool = True) -> None:
        # A metadata-only terminal cannot hide an observed unclosed/gapped item.
        if require_closed and any(not item.closed for item in self.items.values()
                                  if item.kind in {"message", "reasoning"}):
            raise invalid_model_frame()
        text, reasoning = self._prefix(reasoning=False), self._prefix(reasoning=True)
        if text != self.emitted_text or reasoning != self.emitted_reasoning:
            raise invalid_model_frame()
        for key, item in self.items.items():
            if item.kind in {"message", "reasoning"} and key not in self.identities.item_indexes:
                raise invalid_model_frame()
        indexes = sorted(self.identities.indexes)
        if any(index != expected for expected, index in enumerate(indexes)):
            raise invalid_model_frame()
        # An explicit output limit may finish an open prefix, but cannot grant
        # bounded-text authority while already received parts remain buffered.
        for kind, expected in (("message", self.emitted_text),
                               ("reasoning", self.emitted_reasoning)):
            pieces = []
            for _, key in sorted(self.identities.indexes.items()):
                item = self.items.get(key)
                if item is None or item.kind != kind:
                    continue
                for group in (("content",) if kind == "message" else ("summary", "content")):
                    parts = sorted((i, p) for (g, i), p in item.parts.items() if g == group)
                    pieces.extend(part.text for _, part in parts if part.kind != "refusal")
            if "".join(pieces) != expected:
                raise invalid_model_frame()
