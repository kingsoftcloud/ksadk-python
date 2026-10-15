"""Translate validated A2UI tool envelopes into canonical data items."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import DataContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.runtime.adapter import RunHandle

logger = logging.getLogger("ksadk.runtime._runner_adapter.stream_mapping")


def _a2ui_surface_event(
    self: Any,
    handle: RunHandle,
    chunk: Any,
) -> tuple[RuntimeEvent, ...]:
    """Recognize a validated A2UI tool envelope and emit a canonical data-item event.

    The dynamic ``generate_a2ui`` tool returns official v0.9 operations as a
    JSON tool result. Tool results are otherwise opaque to the runtime, which
    would leave AG-UI with nothing to project until a page reload reconstructs
    history. Convert exactly that envelope at the runtime boundary so it is
    streamed, persisted, and replayed like every other A2UI surface.

    In the canonical schema, A2UI surfaces are modeled as ``item_kind="data"``
    items with ``source.protocol="a2ui"``.
    """

    if not isinstance(chunk, dict):
        return ()
    value = chunk.get("tool_output", chunk.get("output"))
    if value is not None and hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, Mapping):
        return ()
    operations_raw = value.get("a2ui_operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        return ()
    operations = [dict(operation) for operation in operations_raw if isinstance(operation, Mapping)]
    if not operations:
        return ()

    known: list[tuple[str, str]] = []  # (surface_id, lifecycle)
    for operation in operations:
        for key, lifecycle in (
            ("createSurface", "begin"),
            ("updateComponents", "update"),
            ("updateDataModel", "update"),
            ("deleteSurface", "end"),
        ):
            detail = operation.get(key)
            if isinstance(detail, Mapping) and isinstance(detail.get("surfaceId"), str):
                surface_id = detail["surfaceId"].strip()
                if surface_id:
                    known.append((surface_id, lifecycle))
                    break
    if not known:
        return ()
    surface_ids = {surface_id for surface_id, _lifecycle in known}
    if len(surface_ids) != 1:
        logger.warning("ignoring A2UI tool result with multiple surfaces")
        return ()
    surface_id = known[0][0]
    lifecycle = "begin" if any(lc == "begin" for _, lc in known) else known[0][1]

    framework = self._runtime_type
    run_id = handle.run_id
    scope_id = stable_scope_id(framework, run_id)
    # A dynamic A2UI tool result is a complete, immutable operation batch.  It
    # must therefore be represented by a closed canonical item.  Reusing one
    # long-lived item per surface leaves a create/update-only batch open at the
    # run terminal and makes strict reducers reject the stream.
    start_seq = self._next_seq()
    batch_ref = str(chunk.get("call_id") or chunk.get("run_id") or chunk.get("id") or "")
    if not batch_ref:
        batch_ref = json.dumps(
            operations,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    item_id = stable_item_id(framework, run_id, "a2ui-batch", surface_id, batch_ref)
    source = SourceRef(
        framework=framework,
        protocol="a2ui",
        native_run_id=run_id,
        metadata={
            "surface_id": surface_id,
            "operation_batch": True,
            "surface_lifecycle": lifecycle,
        },
    )
    # TODO(runtime-event-v2): dict chunk 退化路径,chunk_ordinal 用 seq counter;
    # LangGraph/Codex 切 stream_canonical_events 后清理
    timestamp = time.time()
    snapshot = ContentSnapshot(parts=(DataContent(part_id="a2ui-ops", data=operations),))
    completed_seq = self._next_seq()
    return (
        ItemStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "item.started", "a2ui", run_id, start_seq
            ),
            seq=start_seq,
            timestamp=timestamp,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="data",
            initial=snapshot,
        ),
        ItemCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, item_id, "item.completed", "a2ui", run_id, completed_seq
            ),
            seq=completed_seq,
            timestamp=timestamp,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="data",
            snapshot=snapshot,
        ),
    )
