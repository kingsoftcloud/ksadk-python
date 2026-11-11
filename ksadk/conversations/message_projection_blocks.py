"""历史消息载荷解析与有序正文块投影。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _event_metadata(event: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = event.get("Metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _event_text(event: Mapping[str, Any]) -> str:
    content = event.get("Content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Mapping):
        return ""
    if content.get("text") is not None:
        return str(content.get("text") or "")
    return "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, Mapping)
    )


def _reconcile_part_updates(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Apply canonical replacements to the original text or reasoning chunks in timeline order."""
    rows = list(events)
    chunks_by_part: dict[tuple, list[int]] = {}
    for index, event in enumerate(rows):
        part = event.get("_stream_part")
        if not part:
            continue
        chunks = chunks_by_part.setdefault(part, [])
        text = _event_text(event)
        if not event.get("_replace_part") or not chunks:
            if text:
                chunks.append(index)
            continue

        # A snapshot seals this part only. Preserve its existing positions
        # across intervening tools and other items, including on correction.
        offset = 0
        for position in chunks:
            previous = _event_text(rows[position])
            end = offset + len(previous) if position != chunks[-1] else len(text)
            value = text[offset:end]
            offset = end
            if value != previous:
                rows[position] = {**rows[position], "Content": {"text": value}}
        rows[index] = {**event, "Content": {"text": ""}}
        if not text:
            chunks.clear()
    return rows


def _canonical_message_windows(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Sequence[Mapping[str, Any]]]:
    """Attribute events by message item even when LangGraph seals several items together."""
    windows: dict[str, list[Mapping[str, Any]]] = {}
    completed: set[str] = set()
    pending: list[Mapping[str, Any]] = []
    active = None
    for event in events:
        kind = event.get("EventType")
        if kind not in {"assistant_message", "assistant_stream_delta", "assistant_stream_snapshot"}:
            (windows[active] if active is not None else pending).append(event)
            continue
        item = event.get("_message_item_id")
        if not isinstance(item, str) or not item or item in completed:
            return {}
        if item not in windows:
            windows[item] = pending
            pending = []
            active = item
        windows[item].append(event)
        if kind == "assistant_message":
            completed.add(item)
            if active == item:
                active = None
    if len(completed) != len(windows) or len(windows) < 2:
        return {}
    windows[next(reversed(windows))].extend(pending)
    return windows


def _project_interleaved_blocks(
    events: Sequence[Mapping[str, Any]],
    *,
    tool_events: Sequence[Mapping[str, Any]],
    expected_text: str | None = None,
) -> list[dict[str, Any]]:
    """Reconstruct ordered chat blocks from persisted streaming events.

    ``assistant_stream_snapshot`` carries a cumulative text value.  The delta
    relative to the prior snapshot is the text emitted at that point in the
    event sequence.  Reasoning events written before that snapshot can
    therefore be replayed as ``thinking → text → thinking → text`` instead of
    being flattened into the legacy ``Reasoning`` and ``Content`` fields.

    Older transcripts wrote all reasoning only at terminal time.  Their first
    reasoning event appears after streamed text, so their original boundaries
    are unknowable.  Return no blocks for those transcripts and let clients
    use the existing best-effort compatibility projection.
    """

    blocks: list[dict[str, Any]] = []
    expected_block_text = expected_text
    latest_snapshot_text = ""
    saw_stream_text = False
    saw_reasoning_after_stream_text = False
    tools_by_seq_id = {
        int(tool.get("SeqId") or 0): tool for tool in tool_events if int(tool.get("SeqId") or 0) > 0
    }

    def append_text(text: str, seq_id: Any) -> None:
        if not text:
            return
        if blocks and blocks[-1].get("Type") == "text":
            blocks[-1]["Content"] = f"{blocks[-1].get('Content') or ''}{text}"
            return
        blocks.append({"Type": "text", "Content": text, "SeqId": seq_id})

    def append_thinking(text: str, seq_id: Any) -> None:
        if not text:
            return
        if blocks and blocks[-1].get("Type") == "thinking":
            blocks[-1]["Content"] = f"{blocks[-1].get('Content') or ''}{text}"
            return
        blocks.append({"Type": "thinking", "Content": text, "SeqId": seq_id})

    def append_tool(event: Mapping[str, Any]) -> None:
        tool = tools_by_seq_id.get(int(event.get("SeqId") or 0))
        if not tool:
            return
        blocks.append(
            {
                "Type": "tool",
                "SeqId": tool.get("SeqId"),
                "Name": tool.get("Name") or "tool",
                "Args": tool.get("Args"),
                "Result": tool.get("Result"),
                "Status": tool.get("Status") or "completed",
                "ToolCallId": tool.get("ToolCallId"),
            }
        )

    for event in events:
        event_type = str(event.get("EventType") or "")
        text = _event_text(event)
        seq_id = event.get("SeqId")

        if event_type == "reasoning":
            metadata = _event_metadata(event)
            if (text and saw_stream_text and not event.get("_stream_part")
                    and metadata.get("stream_boundary") not in ("before_text", "before_tool")):
                saw_reasoning_after_stream_text = True
            append_thinking(text, seq_id)
            continue
        if event_type == "tool_call":
            append_tool(event)
            continue
        if event_type == "assistant_stream_delta":
            append_text(text, seq_id)
            latest_snapshot_text += text
            saw_stream_text = saw_stream_text or bool(text)
            continue
        if event_type == "assistant_stream_snapshot":
            if text.startswith(latest_snapshot_text):
                append_text(text[len(latest_snapshot_text) :], seq_id)
            elif text != latest_snapshot_text:
                # A replacement snapshot has no safe incremental boundary.
                # Keep the latest content as a standalone text block instead
                # of duplicating already-replayed output.
                append_text(text, seq_id)
            latest_snapshot_text = text
            saw_stream_text = saw_stream_text or bool(text)
            continue
        if event_type == "assistant_message":
            if latest_snapshot_text and text.startswith(latest_snapshot_text):
                append_text(text[len(latest_snapshot_text) :], seq_id)
            elif not latest_snapshot_text:
                append_text(text, seq_id)
            elif (
                text and text == expected_text
                and blocks[-1]["Type"] in {"thinking", "tool"}
                and sum(e.get("EventType") == "assistant_message" for e in events) == 1
            ):
                # A single final answer after a tool or thought can omit earlier commentary.
                append_text(text, seq_id)
                expected_block_text = latest_snapshot_text + text

    if saw_reasoning_after_stream_text:
        return []
    # A run can contain multiple message items, and a terminal snapshot can
    # replace streamed text. Without reliable boundaries, use the existing
    # Content/Reasoning/ToolEvents projection instead of displaying stale or
    # whole-run text for one message.
    if (
        expected_block_text is not None
        and "".join(block["Content"] for block in blocks if block["Type"] == "text")
        != expected_block_text
    ):
        return []
    return blocks
