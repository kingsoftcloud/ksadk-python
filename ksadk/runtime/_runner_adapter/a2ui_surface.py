"""Validated declared A2UI carriers projected to ordinary canonical data items."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from ksadk.a2ui.registry import current_catalog_registry
from ksadk.a2ui.payload import extract_operations
from ksadk.a2ui.wire import A2uiWireState
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import DataContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.interaction.native_events import NativeInteractionRequested
from ksadk.runtime.adapter import RunHandle

logger = logging.getLogger(__name__)


def bind_legacy_input_surface(self, handle, event):
    """Resolve an explicit legacy reference only against this Run's accepted UI."""
    if not isinstance(event, NativeInteractionRequested) or event.run_id != handle.run_id:
        return
    if (event._presentation or {}).get("a2ui") is not None:
        return
    sid = event._native_target.get("a2ui_surface_id")
    cache = handle.native_ref.get("a2ui_presentation") or {}
    if not isinstance(sid, str) or sid not in cache.get("catalogs", {}):
        return
    if cache.get("interactions", {}).get(sid) not in (None, event.interaction_id):
        return
    messages = [op for op in cache.get("history", []) if any(
        isinstance(value, dict) and value.get("surfaceId") == sid for value in op.values()
    )]
    # A reference without a generation cannot authorize a deleted/recreated UI.
    if any("deleteSurface" in op for op in messages):
        return
    try:
        from ksadk.a2ui.presentation import build_a2ui_presentation

        presentation = build_a2ui_presentation(
            messages, catalog_registry=getattr(self, "a2ui_catalog_registry", None),
        )
    except ValueError:
        return
    event._presentation = {**presentation, **(event._presentation or {})}
    event._native_target["legacy_a2ui_binding"] = True
    cache.setdefault("interactions", {})[sid] = event.interaction_id


def _bind_existing_input_surface(self, handle, event, messages):
    """Promote an unchanged, validated tool presentation to its native wait."""
    cache = handle.native_ref.get("a2ui_presentation")
    if not cache or not isinstance(messages, list):
        return None
    trial = A2uiWireState(getattr(self, "a2ui_catalog_registry", None))
    trial.accept(messages)
    surfaces = list(trial.catalogs)
    for sid in surfaces:
        if cache.get("catalogs", {}).get(sid) != trial.catalogs[sid]:
            return None
        if cache.get("interactions", {}).get(sid) not in (None, event.interaction_id):
            return None
        current = []
        for operation in cache["history"]:
            if any(isinstance(value, dict) and value.get("surfaceId") == sid
                   for value in operation.values()):
                if "createSurface" in operation:
                    current = []
                current.append(operation)
        snapshot = [operation for operation in messages if any(
            isinstance(value, dict) and value.get("surfaceId") == sid for value in operation.values()
        )]
        if current != snapshot:
            return None
    if not surfaces:
        return None
    batch = f"interaction:{event.interaction_id}"
    if batch in cache["seen"]:
        return ()
    cache.setdefault("interactions", {}).update({sid: event.interaction_id for sid in surfaces})
    cache["seen"].append(batch)
    chunk = {"_scope_id": event.scope_id, "_parent_scope_id": event.parent_scope_id,
             "_batch_id": batch, "_presentation_binding": True}
    return tuple(item for sid in surfaces for item in _surface_events(self, handle, chunk, [
        operation for operation in messages if any(
            isinstance(value, dict) and value.get("surfaceId") == sid for value in operation.values()
        )
    ]))


def restore_a2ui_presentation(handle, record, *, registry=None):
    """Rebuild a missing presentation cache from the authorized stored request."""
    a2ui = record.presentation.a2ui if record.presentation is not None else None
    if a2ui is None:
        return
    cache = handle.native_ref.setdefault("a2ui_presentation", {
        "history": [], "seen": [], "catalogs": {},
    })
    missing = {
        op["createSurface"]["surfaceId"] for op in a2ui.messages if "createSurface" in op
        and op["createSurface"]["surfaceId"] not in cache.get("catalogs", {})
    }
    if not missing:
        return
    messages = [op for op in a2ui.messages if any(
        isinstance(value, dict) and value.get("surfaceId") in missing for value in op.values()
    )]
    try:
        state = A2uiWireState(registry)
        state.history = cache["history"]
        state.accept(messages)
        cache["history"] = state.history
        cache.setdefault("catalogs", {}).update(state.catalogs)
        cache.setdefault("interactions", {}).update({sid: record.interaction_id for sid in missing})
    except Exception as exc:
        logger.warning("Stored optional A2UI presentation rejected (%s)", type(exc).__name__)


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

    if isinstance(chunk, NativeInteractionRequested):
        a2ui = (chunk._presentation or {}).get("a2ui")
        if not isinstance(a2ui, dict):
            return ()
        try:
            promoted = _bind_existing_input_surface(self, handle, chunk, a2ui["messages"])
        except (KeyError, ValueError):
            promoted = None
        if promoted is not None:
            return promoted
        return _a2ui_surface_event(self, handle, {
            "tool_output": {"a2ui_operations": a2ui["messages"]},
            "_scope_id": chunk.scope_id, "_parent_scope_id": chunk.parent_scope_id,
            "_batch_id": f"interaction:{chunk.interaction_id}",
            "_interaction_id": chunk.interaction_id,
        })
    if isinstance(chunk, ItemCompleted):
        return tuple(
            event for part in chunk.snapshot.parts if isinstance(part, ToolResultContent)
            for event in _a2ui_surface_event(self, handle, {
                "tool_output": part.result, "call_id": part.call_id,
                "_scope_id": chunk.scope_id, "_parent_scope_id": chunk.parent_scope_id,
                "_batch_id": f"{chunk.event_id}:{part.part_id}",
            })
        )
    if not isinstance(chunk, dict):
        return ()
    value = chunk.get("tool_output", chunk.get("output"))
    try:
        operations = extract_operations(value)
        if not operations:
            return ()
        batch = str(chunk.get("_batch_id") or chunk.get("call_id") or json.dumps(operations))
        cache = handle.native_ref.setdefault("a2ui_presentation", {"history": [], "seen": []})
        if batch in cache["seen"]:
            return ()
        state = A2uiWireState(getattr(self, "a2ui_catalog_registry", None))
        state.history = cache["history"]
        state.accept(operations)
        cache["history"] = state.history
        cache.setdefault("catalogs", {}).update(state.catalogs)
        if chunk.get("_interaction_id"):
            for op in operations:
                for detail in op.values():
                    if isinstance(detail, dict) and isinstance(detail.get("surfaceId"), str):
                        cache.setdefault("interactions", {})[detail["surfaceId"]] = (
                            chunk["_interaction_id"]
                        )
        else:
            for op in operations:
                if "createSurface" in op:
                    cache.get("interactions", {}).pop(op["createSurface"]["surfaceId"], None)
        cache["seen"].append(batch)
    except Exception as exc:
        logger.warning("Declared A2UI payload rejected (%s)", type(exc).__name__)
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
        return tuple(
            event for sid in dict.fromkeys(sid for sid, _ in known)
            for event in _surface_events(self, handle, chunk, [
                op for op in operations if any(
                    isinstance(detail, dict) and detail.get("surfaceId") == sid
                    for detail in op.values()
                )
            ])
        )
    return _surface_events(self, handle, chunk, operations)


def _surface_events(self, handle, chunk, operations):
    known = [(str(detail["surfaceId"]), key) for op in operations
             for key, detail in op.items() if isinstance(detail, dict) and "surfaceId" in detail]
    surface_id = known[0][0]
    registry = getattr(self, "a2ui_catalog_registry", None) or current_catalog_registry()
    catalog_id = handle.native_ref["a2ui_presentation"]["catalogs"].get(surface_id)
    if catalog_id is None:
        catalog_id = next(op["createSurface"]["catalogId"] for op in reversed(
            handle.native_ref["a2ui_presentation"]["history"]
        ) if op.get("createSurface", {}).get("surfaceId") == surface_id)
    profile = registry.describe(catalog_id)
    interaction = handle.native_ref["a2ui_presentation"].get("interactions", {}).get(surface_id)
    lifecycle = (
        "end" if known[-1][1] == "deleteSurface"
        else "begin" if any(key == "createSurface" for _, key in known) else "update"
    )

    framework = self._runtime_type
    run_id = handle.run_id
    scope_id = chunk.get("_scope_id") or stable_scope_id(framework, run_id)
    # A dynamic A2UI tool result is a complete, immutable operation batch.  It
    # must therefore be represented by a closed canonical item.  Reusing one
    # long-lived item per surface leaves a create/update-only batch open at the
    # run terminal and makes strict reducers reject the stream.
    start_seq = self._next_seq()
    batch_ref = str(chunk.get("_batch_id") or chunk.get("call_id") or chunk.get("id") or "")
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
            "surface_lifecycle": "update" if chunk.get("_presentation_binding") else lifecycle,
            **({"presentation_snapshot": True} if chunk.get("_presentation_binding") else {}),
            **profile,
            "mime_type": "application/a2ui+json",
            **({"interaction_id": interaction} if interaction else {}),
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
            parent_scope_id=chunk.get("_parent_scope_id"),
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
            parent_scope_id=chunk.get("_parent_scope_id"),
            source=source,
            item_id=item_id,
            item_kind="data",
            snapshot=snapshot,
        ),
    )
