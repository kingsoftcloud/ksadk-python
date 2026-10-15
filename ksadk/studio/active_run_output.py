"""Restore the visible partial assistant answer for an active Studio run."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ksadk.events.canonical import (
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    parse_runtime_event,
)
from ksadk.events.content import TextContent
from ksadk.studio.contracts import RunEvent, RunRecord


class _RunEventReader(Protocol):
    async def events(self, run_id: str, *, after: int = 0) -> list[RunEvent]: ...


async def active_run_output(run_service: _RunEventReader, run: RunRecord) -> str:
    """Return a finalized output or replay the partial active answer."""

    if run.output:
        return run.output
    events = await run_service.events(run.id)
    return replay_active_message(events, runtime_type=run.runtime_type)


def replay_active_message(events: Iterable[RunEvent], *, runtime_type: str) -> str:
    """Rebuild the latest visible message item from durable canonical events.

    Studio lifecycle rows and canonical runtime rows intentionally coexist in
    the same log. Only canonical message items contribute to the assistant
    answer; reasoning and Harness progress remain in the activity timeline.
    """

    item_parts: dict[tuple[str, str], dict[str, str]] = {}
    latest_item: tuple[str, str] | None = None
    for persisted in events:
        payload = persisted.data.get("runtimeEvent")
        if not isinstance(payload, dict):
            continue
        if (
            runtime_type == "harness"
            and payload.get("source", {}).get("metadata", {}).get("native_event_type")
            == "run.progress"
        ):
            continue
        try:
            event = parse_runtime_event(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(
            event,
            (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted),
        ) or event.item_kind != "message":
            continue

        key = (event.scope_id, event.item_id)
        parts = item_parts.setdefault(key, {})
        if isinstance(event, ItemStarted):
            snapshot = event.initial
            if snapshot is not None:
                _replace_text_parts(parts, snapshot.parts)
        elif isinstance(event, ItemUpdated):
            if not isinstance(event.update, TextContent):
                continue
            previous = parts.get(event.update.part_id, "")
            parts[event.update.part_id] = (
                previous + event.update.text if event.op == "append" else event.update.text
            )
        else:
            _replace_text_parts(parts, event.snapshot.parts)
        if any(parts.values()):
            latest_item = key

    if latest_item is None:
        return ""
    return "".join(item_parts[latest_item].values())


def _replace_text_parts(parts: dict[str, str], snapshot_parts: tuple[object, ...]) -> None:
    parts.clear()
    parts.update(
        (part.part_id, part.text) for part in snapshot_parts if isinstance(part, TextContent)
    )


__all__ = ["active_run_output", "replay_active_message"]
