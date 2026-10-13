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
            if saw_stream_text and metadata.get("stream_boundary") != "before_text":
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

    if saw_reasoning_after_stream_text:
        return []
    # A run can contain multiple message items, and a terminal snapshot can
    # replace streamed text. Without reliable boundaries, use the existing
    # Content/Reasoning/ToolEvents projection instead of displaying stale or
    # whole-run text for one message.
    if expected_text is not None and "".join(
        block["Content"] for block in blocks if block["Type"] == "text"
    ) != expected_text:
        return []
    return blocks
