"""canonical-v2 to legacy v1 wire projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ksadk.events._v1_compat.models import (
    A2ATaskProjectionRef,
    A2UIInteractionProjectionRef,
    A2UISurfaceProjectionRef,
    EventTypeV1,
    RuntimeEventV1,
    RuntimeEventV1ProjectionContext,
    RuntimeEventV1ProjectionMode,
    V1ProjectionContextRequiredError,
)
from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationResumed,
    EventPhase,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    UsageReported,
)
from ksadk.events.content import (
    ArtifactContent,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)


def _phase_for_item(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> EventPhase:
    if event.item_kind == "reasoning":
        return "commentary"
    if event.item_kind != "message":
        raise ValueError(f"item kind {event.item_kind!r} has no v1 text phase")
    if isinstance(event, ItemStarted) and event.phase is not None:
        return event.phase
    item = context.item(event.scope_id, event.item_id) if context else None
    phase = item.phase if item is not None else None
    if phase is None:
        raise V1ProjectionContextRequiredError(
            "message phase requires RuntimeEventV1ProjectionContext"
        )
    return phase


def _source_event_id(event: RuntimeEvent) -> str:
    return event.source.native_event_id or event.event_id


def _identity_payload(
    event: RuntimeEvent,
    *,
    item_id: str | None = None,
    part_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_id": event.scope_id,
        "source_event_id": _source_event_id(event),
    }
    if item_id is not None:
        payload["item_id"] = item_id
    if part_id is not None:
        payload["part_id"] = part_id
    return payload


def _artifact_payload(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    part: ArtifactContent,
    context: RuntimeEventV1ProjectionContext | None,
) -> dict[str, Any]:
    if context is None:
        raise V1ProjectionContextRequiredError(
            "artifact version requires RuntimeEventV1ProjectionContext"
        )
    return {
        "name": part.name,
        "version": context.artifact_version(event.scope_id, event.item_id, part.artifact_id),
        "uri": part.uri,
        "mime": part.mime_type,
        "data": part.data,
        **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
    }


def _a2a_task_ref(
    event: RuntimeEvent,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2ATaskProjectionRef | None:
    if event.source.framework != "a2a":
        return None
    ref = context.a2a_tasks.get((event.run_id, event.scope_id)) if context else None
    if ref is None or not ref.task_id.strip() or not ref.origin.strip():
        raise V1ProjectionContextRequiredError(
            "A2A task projection requires nonempty task_id and origin"
        )
    return ref


def _a2ui_surface_ref(
    event: ItemStarted | ItemUpdated | ItemSnapshotReplaced | ItemCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2UISurfaceProjectionRef | None:
    ref = context.a2ui_surfaces.get((event.scope_id, event.item_id)) if context else None
    if ref is not None and not ref.surface_id.strip():
        raise V1ProjectionContextRequiredError(
            "A2UI surface projection requires a nonempty surface_id"
        )
    return ref


def _a2ui_interaction_ref(
    event: InteractionRequested | InteractionResolved,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2UIInteractionProjectionRef | None:
    ref = context.a2ui_interactions.get((event.scope_id, event.interaction_id)) if context else None
    if ref is not None and not ref.surface_id.strip():
        raise V1ProjectionContextRequiredError(
            "A2UI interaction projection requires a nonempty surface_id"
        )
    return ref


def _project_artifact_parts(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    parts: tuple[ArtifactContent, ...],
    *,
    generic_event_type: str,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    a2a_ref = _a2a_task_ref(event, context)
    event_type = EventTypeV1.A2A_TASK_ARTIFACT if a2a_ref is not None else generic_event_type
    projected: list[RuntimeEventV1] = []
    for ordinal, part in enumerate(parts):
        artifact = _artifact_payload(event, part, context)
        payload = (
            {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "artifact": artifact,
                **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            }
            if a2a_ref is not None
            else artifact
        )
        projected.append(
            _v1_event(
                event,
                event_type,
                payload,
                context=context,
                ordinal=ordinal,
            )
        )
    return tuple(projected)


def _v1_event(
    event: RuntimeEvent,
    event_type: str,
    payload: dict[str, Any],
    *,
    context: RuntimeEventV1ProjectionContext | None,
    phase: EventPhase | None = None,
    ordinal: int = 0,
    identity_item_id: str | None = None,
    identity_part_id: str | None = None,
) -> RuntimeEventV1:
    if context is None or not all(
        value.strip() for value in (context.agent_id, context.user_id, context.session_id)
    ):
        raise V1ProjectionContextRequiredError(
            "v1 output requires a complete nonempty envelope context"
        )
    if context.projection is not None and context.projection.run_id != event.run_id:
        raise V1ProjectionContextRequiredError(
            "RuntimeEventV1ProjectionContext projection run_id must match event run_id"
        )
    item_id = identity_item_id or payload.get("item_id") or ""
    part_id = identity_part_id or payload.get("part_id") or ""
    identity = json.dumps(
        [event.event_id, ordinal, item_id, part_id, event_type],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_event_id = f"evt_v1_{hashlib.sha256(identity).hexdigest()[:32]}"
    projected = RuntimeEventV1(
        event_id=legacy_event_id,
        event_type=event_type,
        timestamp=event.timestamp,
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
        invocation_id=event.run_id,
        seq_id=event.seq,
        phase=phase,
        payload=payload,
    )
    projected.validate_conformance()
    return projected


def _project_text_item(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    if event.item_kind not in {"message", "reasoning"}:
        return ()
    if mode == "snapshot_only":
        return ()
    parts: tuple[TextContent, ...]
    if isinstance(event, ItemUpdated):
        if not isinstance(event.update, TextContent):
            return ()
        parts = (event.update,)
        completed = False
        operation = event.op
    elif isinstance(event, ItemCompleted):
        parts = tuple(part for part in event.snapshot.parts if isinstance(part, TextContent))
        completed = True
        operation = "replace"
    else:
        if event.initial is None:
            return ()
        parts = tuple(part for part in event.initial.parts if isinstance(part, TextContent))
        completed = False
        operation = "replace"
    if not parts:
        return ()
    phase = _phase_for_item(event, context)
    prefix = "reasoning" if event.item_kind == "reasoning" else "text"
    event_type = f"{prefix}.completed" if completed else f"{prefix}.delta"
    projected: list[RuntimeEventV1] = []
    for ordinal, part in enumerate(parts):
        payload = {
            "text": part.text,
            **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            "operation": operation,
        }
        projected.append(
            _v1_event(
                event,
                event_type,
                payload,
                context=context,
                phase=phase,
                ordinal=ordinal,
            )
        )
    return tuple(projected)


def _project_item_started(
    event: ItemStarted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            return ()
        data_parts = (
            tuple(part for part in event.initial.parts if isinstance(part, DataContent))
            if event.initial is not None
            else ()
        )
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": [part.data for part in data_parts],
            **_identity_payload(event, item_id=event.item_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_BEGIN, payload, context=context),)
    if event.item_kind == "artifact" and event.initial is not None:
        artifact_parts = tuple(
            part for part in event.initial.parts if isinstance(part, ArtifactContent)
        )
        return _project_artifact_parts(
            event,
            artifact_parts,
            generic_event_type=EventTypeV1.ARTIFACT_CREATED,
            context=context,
        )
    if event.item_kind != "tool_call" or event.initial is None:
        return ()
    tool_parts = tuple(part for part in event.initial.parts if isinstance(part, ToolCallContent))
    return tuple(
        _v1_event(
            event,
            EventTypeV1.TOOL_CALL_BEGIN,
            {
                "call_id": part.call_id,
                "name": part.name,
                "args": part.arguments,
                **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            },
            context=context,
            ordinal=ordinal,
        )
        for ordinal, part in enumerate(tool_parts)
    )


def _project_item_updated(
    event: ItemUpdated,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None or not isinstance(event.update, DataContent):
            return ()
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": event.update.data,
            **_identity_payload(event, item_id=event.item_id, part_id=event.update.part_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_UPDATE, payload, context=context),)
    if event.item_kind != "artifact" or not isinstance(event.update, ArtifactContent):
        return ()
    return _project_artifact_parts(
        event,
        (event.update,),
        generic_event_type=EventTypeV1.ARTIFACT_UPDATED,
        context=context,
    )


def _project_item_completed(
    event: ItemCompleted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            return ()
        parts = tuple(part for part in event.snapshot.parts if isinstance(part, DataContent))
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": [part.data for part in parts],
            **_identity_payload(event, item_id=event.item_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_END, payload, context=context),)
    if event.item_kind == "tool_result":
        tool_result_parts = tuple(
            part for part in event.snapshot.parts if isinstance(part, ToolResultContent)
        )
        return tuple(
            _v1_event(
                event,
                EventTypeV1.TOOL_CALL_END,
                {
                    "call_id": part.call_id,
                    "name": (context.tool_name(event.scope_id, part.call_id) if context else ""),
                    "result": part.result,
                    "error": part.result if part.is_error else None,
                    **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
                },
                context=context,
                ordinal=ordinal,
            )
            for ordinal, part in enumerate(tool_result_parts)
        )
    if event.item_kind == "artifact":
        artifact_parts = tuple(
            part for part in event.snapshot.parts if isinstance(part, ArtifactContent)
        )
        return _project_artifact_parts(
            event,
            artifact_parts,
            generic_event_type=EventTypeV1.ARTIFACT_UPDATED,
            context=context,
        )
    return ()


def _project_item_snapshot_replaced(
    event: ItemSnapshotReplaced,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    """Project only snapshots with an existing lossless v1 item-level meaning."""

    if event.item_kind in {"message", "reasoning"}:
        if mode == "snapshot_only":
            return ()
        raise V1ProjectionContextRequiredError(
            "identity_replace cannot represent an item-level snapshot replacement "
            "without leaving stale or reordered v1 text parts"
        )

    if event.item_kind == "data" and event.source.protocol == "a2ui":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            raise V1ProjectionContextRequiredError(
                "A2UI item-level snapshot requires a typed surface projection ref"
            )
        raise V1ProjectionContextRequiredError(
            "v1 A2UI updates cannot represent an item-level snapshot replacement atomically"
        )

    # A2A and artifact projection identities must still be validated before the
    # legacy boundary rejects a snapshot it cannot express atomically.
    _a2a_task_ref(event, context)
    if event.item_kind == "artifact":
        if context is None:
            raise V1ProjectionContextRequiredError(
                "artifact item-level snapshot requires typed projection context"
            )
        for part in event.snapshot.parts:
            if not isinstance(part, ArtifactContent):
                raise V1ProjectionContextRequiredError(
                    "artifact item-level snapshot contains incompatible content"
                )
            context.artifact_version(event.scope_id, event.item_id, part.artifact_id)
        raise V1ProjectionContextRequiredError(
            "v1 artifact events cannot represent an item-level snapshot replacement atomically"
        )

    if mode == "identity_replace":
        raise V1ProjectionContextRequiredError(
            f"v1 cannot represent an item-level snapshot replacement for {event.item_kind!r}"
        )
    return ()


def _snapshot_output_events(
    event: RunCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    if context is None or context.projection is None:
        raise V1ProjectionContextRequiredError(
            "snapshot_only run completion requires reducer RunProjection"
        )
    if context.projection.run_id != event.run_id:
        raise V1ProjectionContextRequiredError(
            "RunProjection run_id must match run.completed run_id"
        )
    if context.projection.status != "completed":
        raise V1ProjectionContextRequiredError(
            "RunProjection status must be completed for snapshot_only output"
        )
    if context.projection.output_refs != event.output_refs:
        raise V1ProjectionContextRequiredError(
            "RunProjection output_refs must match run.completed output_refs"
        )
    projected: list[RuntimeEventV1] = []
    ordinal = 0
    for output_ref in event.output_refs:
        item = context.item(output_ref.scope_id, output_ref.item_id)
        if item is None:
            raise V1ProjectionContextRequiredError(
                "RunProjection is missing a run.completed output_ref item"
            )
        if item.item_kind not in {"message", "reasoning"}:
            continue
        if item.item_kind == "reasoning":
            phase: EventPhase = "commentary"
            event_type = EventTypeV1.REASONING_COMPLETED
        else:
            if item.phase is None:
                raise V1ProjectionContextRequiredError(
                    "message phase is missing from reducer RunProjection"
                )
            phase = item.phase
            event_type = EventTypeV1.TEXT_COMPLETED
        parts = tuple(part for part in item.parts if isinstance(part, TextContent))
        if output_ref.part_id is not None:
            parts = tuple(part for part in parts if part.part_id == output_ref.part_id)
            if not parts:
                raise V1ProjectionContextRequiredError(
                    "RunProjection is missing a run.completed output_ref part"
                )
        for part in parts:
            projected.append(
                _v1_event(
                    event,
                    event_type,
                    {"text": part.text},
                    context=context,
                    phase=phase,
                    ordinal=ordinal,
                    identity_item_id=item.item_id,
                    identity_part_id=part.part_id,
                )
            )
            ordinal += 1
    return tuple(projected)


def _project_run_event(
    event: RuntimeEvent,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...] | None:
    event_type: str
    payload: dict[str, Any]
    a2a_ref = _a2a_task_ref(event, context)
    snapshot_events: tuple[RuntimeEventV1, ...] = ()
    if isinstance(event, RunCompleted) and mode == "snapshot_only":
        snapshot_events = _snapshot_output_events(event, context)
    if a2a_ref is not None:
        if isinstance(event, RunStarted):
            event_type = EventTypeV1.A2A_TASK_CREATED
            payload = {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "status": event.status,
            }
        elif isinstance(
            event,
            (RunProgress, RunInterrupted, RunCompleted, RunFailed, RunCanceled),
        ):
            event_type = EventTypeV1.A2A_TASK_STATUS
            payload = {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "status": event.status,
            }
            if isinstance(event, RunFailed):
                payload["error"] = event.error.model_dump(mode="json")
        else:
            return None
        payload.update(_identity_payload(event))
        lifecycle = _v1_event(
            event,
            event_type,
            payload,
            context=context,
            ordinal=len(snapshot_events),
        )
        return (*snapshot_events, lifecycle)
    if isinstance(event, RunStarted):
        event_type, payload = EventTypeV1.RUN_STARTED, {"status": event.status}
    elif isinstance(event, RunProgress):
        event_type = EventTypeV1.RUN_PROGRESS
        payload = {"status": event.status, "progress": event.progress, "message": event.message}
    elif isinstance(event, RunInterrupted):
        event_type = EventTypeV1.RUN_INTERRUPTED
        payload = {
            "status": event.status,
            "reason": event.reason,
            "interaction_id": event.interaction_id,
            "continuation_id": event.continuation_id,
        }
    elif isinstance(event, RunCompleted):
        event_type = EventTypeV1.RUN_COMPLETED
        payload = {
            "status": event.status,
            "output_refs": [
                ref.model_dump(mode="json", exclude_none=True) for ref in event.output_refs
            ],
        }
    elif isinstance(event, RunFailed):
        event_type = EventTypeV1.RUN_FAILED
        payload = {"status": event.status, "error": event.error.model_dump(mode="json")}
    elif isinstance(event, RunCanceled):
        event_type = EventTypeV1.RUN_CANCELED
        payload = {"status": event.status, "reason": event.reason}
    else:
        return None
    payload.update(_identity_payload(event))
    lifecycle = _v1_event(
        event,
        event_type,
        payload,
        context=context,
        ordinal=len(snapshot_events),
    )
    return (*snapshot_events, lifecycle)


def project_to_v1(
    event: RuntimeEvent,
    *,
    mode: RuntimeEventV1ProjectionMode = "snapshot_only",
    context: RuntimeEventV1ProjectionContext | None = None,
) -> tuple[RuntimeEventV1, ...]:
    """Project one canonical event to zero or more legacy v1 wire events.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``，执行形态为
    ``tests/protocol/test_cross_projection_golden.py``）：
    - RuntimeEventV1 事件类型与各类型 payload（approval_id/call_id/kind/detail、
      surface_id/block_id/data、output_refs、status/error/reason 等）；
    - 身份字段 run_id/scope_id/item_id。

    内部不保证字段：seq/run_seq 的具体数值（仅保序）、source.native_* 游标、
    source.metadata 原始键值。消费方不得依赖未列出的 payload 附加键。
    """

    if mode not in {"snapshot_only", "identity_replace"}:
        raise ValueError(f"unknown RuntimeEvent v1 projection mode: {mode!r}")
    if (
        context is not None
        and context.projection is not None
        and context.projection.run_id != event.run_id
    ):
        raise V1ProjectionContextRequiredError(
            "RuntimeEventV1ProjectionContext projection run_id must match event run_id"
        )

    run_projection = _project_run_event(event, mode=mode, context=context)
    if run_projection is not None:
        return run_projection
    if isinstance(event, ItemStarted):
        return _project_item_started(event, mode=mode, context=context)
    if isinstance(event, ItemUpdated):
        return _project_item_updated(event, mode=mode, context=context)
    if isinstance(event, ItemSnapshotReplaced):
        return _project_item_snapshot_replaced(event, mode=mode, context=context)
    if isinstance(event, ItemCompleted):
        return _project_item_completed(event, mode=mode, context=context)
    if isinstance(event, ItemFailed):
        return ()
    if isinstance(event, InteractionRequested):
        a2ui_ref = _a2ui_interaction_ref(event, context)
        if a2ui_ref is not None:
            payload = {
                "surface_id": a2ui_ref.surface_id,
                "block_id": a2ui_ref.block_id,
                "data": event.request.model_dump(mode="json", by_alias=True),
                **_identity_payload(event, item_id=event.interaction_id),
            }
            return (_v1_event(event, EventTypeV1.A2UI_INTERACTION, payload, context=context),)
        if event.interaction_kind != "approval" or event.request.request_type != "approval":
            return ()
        call_id = event.request.call_id or (
            context.interaction_call_id(event.scope_id, event.interaction_id) if context else ""
        )
        payload = {
            "approval_id": event.interaction_id,
            "call_id": call_id,
            "kind": event.request.kind,
            "detail": event.request.detail,
            **_identity_payload(event, item_id=event.interaction_id),
        }
        return (_v1_event(event, EventTypeV1.APPROVAL_REQUESTED, payload, context=context),)
    if isinstance(event, InteractionResolved):
        a2ui_ref = _a2ui_interaction_ref(event, context)
        if a2ui_ref is not None:
            payload = {
                "surface_id": a2ui_ref.surface_id,
                "block_id": a2ui_ref.block_id,
                "data": event.response.model_dump(mode="json", by_alias=True),
                **_identity_payload(event, item_id=event.interaction_id),
            }
            return (_v1_event(event, EventTypeV1.A2UI_ACTION, payload, context=context),)
        if event.interaction_kind != "approval" or event.response.response_type != "approval":
            return ()
        call_id = (
            context.interaction_call_id(event.scope_id, event.interaction_id) if context else ""
        )
        payload = {
            "approval_id": event.interaction_id,
            "call_id": call_id,
            "decision": event.response.decision,
            "data": event.response.data,
            **_identity_payload(event, item_id=event.interaction_id),
        }
        return (_v1_event(event, EventTypeV1.APPROVAL_RESOLVED, payload, context=context),)
    if isinstance(event, ContinuationCreated):
        if event.continuation_kind != "graph_checkpoint":
            return ()
        payload = {
            "checkpoint_id": event.continuation_id,
            "granularity": event.ref.get("granularity", "snapshot"),
            "resume_target": event.ref,
            "resumable": event.resumable,
            **_identity_payload(event, item_id=event.continuation_id),
        }
        return (_v1_event(event, EventTypeV1.CHECKPOINT_CREATED, payload, context=context),)
    if isinstance(event, ContinuationResumed):
        if event.continuation_kind != "graph_checkpoint":
            return ()
        payload = {
            "checkpoint_id": event.continuation_id,
            "resume_attempt_id": event.resume_attempt_id,
            **_identity_payload(event, item_id=event.continuation_id),
        }
        return (_v1_event(event, EventTypeV1.CHECKPOINT_RESUMED, payload, context=context),)
    if isinstance(event, ContextCompactionStarted):
        payload = {
            "phase": context.compaction_phase if context else "runtime",
            "trigger": event.trigger,
            **_identity_payload(event),
        }
        return (_v1_event(event, EventTypeV1.CONTEXT_COMPACTION_STARTED, payload, context=context),)
    if isinstance(event, ContextCompactionCompleted):
        payload = {
            "phase": context.compaction_phase if context else "runtime",
            "trigger": event.trigger,
            "compacted_until_seq_id": event.compacted_until_seq,
            **_identity_payload(event),
        }
        return (
            _v1_event(event, EventTypeV1.CONTEXT_COMPACTION_COMPLETED, payload, context=context),
        )
    if isinstance(event, UsageReported):
        payload = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
            "cached_tokens": event.cached_tokens,
            "reasoning_tokens": event.reasoning_tokens,
            **_identity_payload(event),
        }
        return (_v1_event(event, EventTypeV1.USAGE_REPORTED, payload, context=context),)
    raise TypeError(f"unsupported canonical RuntimeEvent: {type(event).__name__}")


__all__ = ["project_to_v1"]
