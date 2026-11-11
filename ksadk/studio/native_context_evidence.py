"""Read-only Native context facts from Studio's persisted canonical projection.

Counts describe committed compactions, not token accounting or summary quality.
Tool results, partial chunks and unbound/foreign events are not evidence.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from ksadk.events.canonical import ItemCompleted, parse_runtime_event
from ksadk.events.content import DataContent
from ksadk.studio.contracts import RunEvent, RunRecord

_KINDS = {
    "context.compaction_started": "started",
    "context.compaction_completed": "completed",
    "context.compaction_failed": "failed",
}


def native_compaction_evidence(record: RunRecord, events: Iterable[RunEvent]) -> dict | None:
    handle = record.runtime_handle
    native_ref = handle.get("native_ref")
    if (
        record.runtime_type != "harness"
        or handle.get("runtime_type") != "harness"
        or not isinstance(native_ref, dict)
        or native_ref.get("engine_kind") != "native"
    ):
        return None
    native_run_id = handle.get("run_id")
    if not isinstance(native_run_id, str) or not native_run_id:
        return None
    counts = dict(started=0, completed=0, failed=0)
    seen: set[str] = set()
    for wrapper in events:
        if (
            wrapper.run_id != record.id
            or wrapper.type != "item.completed"
            or wrapper.data.get("runId") != record.id
            or wrapper.data.get("runtimeRunId") != native_run_id
        ):
            continue
        raw = wrapper.data.get("runtimeEvent")
        if not isinstance(raw, dict) or raw.get("item_kind") != "status":
            continue
        try:
            event = parse_runtime_event(raw)
        except (ValidationError, ValueError, TypeError):
            continue
        if not isinstance(event, ItemCompleted):
            continue
        source = event.source
        kind = source.metadata.get("native_event_kind")
        if (
            not isinstance(kind, str)
            or kind not in _KINDS
            or event.run_id != native_run_id
            or source.native_run_id != native_run_id
            or wrapper.data.get("scopeId") != event.scope_id
            or source.framework != "ksadk"
            or source.metadata.get("engine_kind") != "native"
            or source.metadata.get("runtime_type") != "harness"
            or not source.native_event_id
            or source.native_event_id in seen
        ):
            continue
        if not any(
            isinstance(part, DataContent)
            and part.part_id == "status"
            and isinstance(part.data, dict)
            and part.data.get("native_event_kind") == kind
            for part in event.snapshot.parts
        ):
            continue
        seen.add(source.native_event_id)
        counts[_KINDS[kind]] += 1
    return {"source": "native_committed_events", **counts} if seen else None
