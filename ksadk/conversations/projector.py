"""One identity-aware RuntimeEvent -> ConversationItem projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ksadk.conversations.contracts import ConversationItem
from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationResumed,
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
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.kernel.contracts import SessionEventEnvelope

_INTERACTION_EVENT_TYPES = frozenset(
    {
        "interaction.requested",
        "interaction.resolved",
        "interaction.cancelled",
        "interaction.expired",
    }
)
_INTERACTION_KINDS = frozenset({"approval", "structured_input", "plan_review", "custom"})


def project_conversation_item(
    event: RuntimeEvent,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> ConversationItem:
    """Create a stable item projection without changing RuntimeEvent truth."""

    item_id = _item_id(event)
    kind = "unknown"
    operation = "append"
    lifecycle = "streaming"
    # Known conversation kinds render in the UI.  Only truly unhandled
    # events (kind stays "unknown" after the branch chain) are hidden so
    # they stay observable via the raw event log without spamming cards.
    visibility = "public"
    schema = "conversation.item.unknown/v1"
    payload: dict[str, Any] = {}
    capability_ref: str | None = None
    if isinstance(event, ItemStarted) and event.item_kind in {"message", "reasoning"}:
        kind = "reasoning" if event.item_kind == "reasoning" else "assistant_text"
        lifecycle = "pending"
        schema = f"conversation.item.{kind}/v1"
        payload = {"text": _text_from_snapshot(event.initial)}
    elif isinstance(event, ItemUpdated) and event.item_kind in {"message", "reasoning"}:
        kind = "reasoning" if event.item_kind == "reasoning" else "assistant_text"
        operation = event.op
        schema = f"conversation.item.{kind}/v1"
        payload = {"text": _text_from_update(event)}
    elif isinstance(event, ItemSnapshotReplaced) and event.item_kind in {"message", "reasoning"}:
        kind = "reasoning" if event.item_kind == "reasoning" else "assistant_text"
        operation = "replace"
        schema = f"conversation.item.{kind}/v1"
        payload = {"text": _text_from_snapshot(event.snapshot)}
    elif isinstance(event, ItemCompleted) and event.item_kind in {"message", "reasoning"}:
        kind = "reasoning" if event.item_kind == "reasoning" else "assistant_text"
        operation = "completed"
        lifecycle = "completed"
        schema = f"conversation.item.{kind}/v1"
        payload = {"text": _text_from_snapshot(event.snapshot)}
    elif isinstance(event, ItemStarted) and event.item_kind == "tool_call":
        kind = "tool_call"
        schema = "conversation.item.tool-call/v1"
        payload = _tool_payload(event.initial)
        capability_ref = "tool.inspect"
    elif isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
        kind = "tool_call"
        operation = "completed"
        lifecycle = "completed"
        schema = "conversation.item.tool-call/v1"
        payload = _tool_payload(event.snapshot)
        capability_ref = "tool.inspect"
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "tool_result"
    ):
        # Codex emits a separate tool_result item carrying the ToolResultContent.
        # Project it as a completed tool_call so the UI renders the output instead
        # of degrading it to an unknown fallback card.
        kind = "tool_call"
        schema = "conversation.item.tool-call/v1"
        capability_ref = "tool.inspect"
        if isinstance(event, ItemStarted):
            payload = _tool_payload(event.initial)
        else:
            payload = _tool_payload(_item_snapshot(event))
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
        elif isinstance(event, ItemSnapshotReplaced):
            operation = "replace"
    elif _is_codex_plan_item(event):
        kind = "plan"
        schema = "conversation.item.plan/v1"
        payload = {"text": _text_from_item_event(event)}
        capability_ref = "plan"
        if isinstance(event, ItemSnapshotReplaced):
            operation = "replace"
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif _is_codex_goal_item(event):
        kind = "goal"
        schema = "conversation.item.goal/v1"
        payload = _goal_payload(_item_snapshot(event))
        capability_ref = "goal"
        if isinstance(event, ItemSnapshotReplaced):
            operation = "replace"
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "artifact"
    ):
        kind = "artifact"
        schema = "conversation.item.artifact/v1"
        payload = _artifact_payload(_item_snapshot(event))
        if isinstance(event, ItemSnapshotReplaced):
            operation = "replace"
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "data"
        and event.source.protocol == "a2ui"
    ):
        kind = "a2ui"
        schema = "conversation.item.a2ui/v1"
        payload = _data_payload(_item_snapshot(event))
        if isinstance(event, ItemSnapshotReplaced):
            operation = "replace"
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "status"
    ):
        # LangGraph subgraph lifecycle and similar status items.  Project as
        # progress so the UI shows a subtle indicator instead of an unknown card.
        kind = "progress"
        schema = "conversation.item.progress/v1"
        payload = _data_payload(_item_snapshot(event))
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif isinstance(event, ItemFailed):
        kind = "error"
        operation = "completed"
        lifecycle = "failed"
        schema = "conversation.item.error/v1"
        payload = {"error": event.error.message or event.error.code}
    elif isinstance(event, InteractionRequested):
        kind = "approval" if event.interaction_kind == "approval" else "progress"
        lifecycle = "pending"
        schema = (
            "conversation.item.approval/v1"
            if kind == "approval"
            else "conversation.item.structured-input/v1"
        )
        payload = {
            "interactionId": event.interaction_id,
            "kind": getattr(event.request, "kind", event.interaction_kind),
            "detail": getattr(event.request, "detail", None),
            "prompt": getattr(event.request, "prompt", None),
            "inputSchema": getattr(event.request, "schema_", None),
            "surfaceId": event.source.metadata.get("surface_id"),
        }
        capability_ref = "approval" if kind == "approval" else "structured_input"
    elif isinstance(event, InteractionResolved):
        kind = "approval" if event.interaction_kind == "approval" else "progress"
        operation = "completed"
        lifecycle = "completed"
        schema = (
            "conversation.item.approval/v1"
            if kind == "approval"
            else "conversation.item.structured-input/v1"
        )
        payload = {
            "interactionId": event.interaction_id,
            "surfaceId": event.source.metadata.get("surface_id"),
        }
        capability_ref = "approval" if kind == "approval" else "structured_input"
    elif isinstance(event, (RunStarted, RunProgress, RunCompleted, RunInterrupted)):
        kind = "progress"
        schema = "conversation.item.progress/v1"
        if isinstance(event, RunProgress):
            payload = {"progress": event.progress, "message": event.message}
        elif isinstance(event, RunInterrupted):
            payload = {"reason": event.reason or ""}
            operation = "completed"
            lifecycle = "completed"
        elif isinstance(event, RunCompleted):
            operation = "completed"
            lifecycle = "completed"
        else:
            payload = {"status": "started"}
    elif isinstance(event, (RunFailed, RunCanceled)):
        kind = "error"
        operation = "completed"
        lifecycle = "failed"
        schema = "conversation.item.error/v1"
        payload = {
            "error": event.error.message if isinstance(event, RunFailed) else event.reason or ""
        }
    elif isinstance(event, (ContinuationCreated, ContinuationResumed)):
        kind = "progress"
        schema = "conversation.item.checkpoint/v1"
        payload = {"continuationId": event.continuation_id}
    elif isinstance(event, UsageReported):
        # Token usage is carried in the top-level SSE envelope; no need to
        # render a conversation card for it.  Project as hidden progress.
        kind = "progress"
        schema = "conversation.item.progress/v1"
        visibility = "hidden"
        payload = {
            "message": "usage",
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "totalTokens": event.total_tokens,
            "cachedTokens": event.cached_tokens,
            "reasoningTokens": event.reasoning_tokens,
        }
        operation = "completed"
        lifecycle = "completed"
    elif isinstance(event, (ContextCompactionStarted, ContextCompactionCompleted)):
        kind = "progress"
        schema = "conversation.item.progress/v1"
        payload = {"message": "context_compaction"}
        if isinstance(event, ContextCompactionCompleted):
            operation = "completed"
            lifecycle = "completed"
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "data"
    ):
        # Catch-all for Codex data items that are not plan/goal/a2ui
        # (fileChange, userMessage, hookPrompt, contextCompaction, etc.).
        # Project as progress so the UI shows a subtle indicator instead of
        # rendering an ugly "unsupported content" card for every turn.
        kind = "progress"
        schema = "conversation.item.progress/v1"
        native_kind = event.source.metadata.get("native_item_kind", "")
        payload = {"message": native_kind or "data", **_data_payload(_item_snapshot(event))}
        if isinstance(event, ItemCompleted):
            operation = "completed"
            lifecycle = "completed"
    # Unhandled event types fall through with kind == "unknown".  They are
    # hidden unless a trusted projector explicitly marks the event as a public
    # conversation surface.  Public future kinds receive the fixed safe
    # fallback card; internal provider chatter remains trace/replay-only.
    if kind == "unknown":
        visibility = (
            "public"
            if event.source.metadata.get("conversation_visibility") == "public"
            else "hidden"
        )
        if visibility == "public":
            payload = {
                "eventType": event.event_type,
                "summary": "This content requires a newer renderer.",
            }
    return ConversationItem(
        item_id=item_id,
        source_event_ids=(event.event_id,),
        session_id=session_id or event.scope_id,
        # A host may own a public/durable Run identity that differs from the
        # provider-native handle.  Conversation clients need the public id for
        # replay and control; the untouched RuntimeEvent remains the native
        # source of truth and is carried separately by the host projection.
        run_id=run_id or event.run_id,
        kind=kind,
        operation=operation,
        lifecycle=lifecycle,
        visibility=visibility,
        payload_schema_ref=schema,
        payload=payload,
        capability_ref=capability_ref,
        native_ref=_native_ref(event),
    )


def project_interaction_conversation_item(
    envelope: SessionEventEnvelope,
) -> ConversationItem | None:
    """Project one authoritative Interaction/v1 fact for a conversation surface.

    Interaction revision is the CAS token for ``SubmitInteraction``.  It is
    therefore read only from the durable Interaction/v1 payload and never
    synthesized from cursor order, lifecycle, or a provider-native event.
    Malformed legacy rows remain observable through their existing Studio
    projection, but cannot become a writable ConversationItem.
    """

    if envelope.family != "interaction" or envelope.family_version != 1:
        return None
    payload = envelope.payload
    event_type = envelope.event_type
    if event_type not in _INTERACTION_EVENT_TYPES:
        return None
    if payload.get("event_type") != event_type:
        return None

    interaction_id = _nonempty_string(payload.get("interaction_id"))
    session_id = _nonempty_string(payload.get("session_id"))
    run_id = _nonempty_string(payload.get("run_id"))
    interaction_kind = _nonempty_string(payload.get("kind"))
    revision = _positive_revision(payload.get("revision"))
    if (
        interaction_id is None
        or session_id != envelope.session_id
        or run_id is None
        or interaction_kind not in _INTERACTION_KINDS
        or revision is None
    ):
        return None
    if envelope.run_id is not None and run_id != envelope.run_id:
        return None

    is_requested = event_type == "interaction.requested"
    request = payload.get("request")
    if is_requested and not isinstance(request, Mapping):
        return None
    request_payload = request if isinstance(request, Mapping) else {}
    presentation = request_payload.get("presentation")
    presentation_payload = presentation if isinstance(presentation, Mapping) else {}

    item_payload: dict[str, Any] = {
        "interactionId": interaction_id,
        "interactionKind": interaction_kind,
        "kind": interaction_kind,
        "revision": revision,
    }
    if is_requested:
        item_payload.update(
            {
                "kind": _nonempty_string(presentation_payload.get("title")) or interaction_kind,
                "inputSchema": (
                    dict(request_payload.get("request_schema"))
                    if isinstance(request_payload.get("request_schema"), Mapping)
                    else {}
                ),
                "createdAt": str(payload.get("timestamp") or envelope.timestamp),
            }
        )
        title = _nonempty_string(presentation_payload.get("title"))
        detail = _nonempty_string(presentation_payload.get("description"))
        expires_at = _nonempty_string(request_payload.get("expires_at"))
        if title is not None:
            item_payload["title"] = title
        if detail is not None:
            item_payload["detail"] = detail
            item_payload["prompt"] = detail
        if expires_at is not None:
            item_payload["expiresAt"] = expires_at
        if presentation_payload:
            item_payload["presentation"] = dict(presentation_payload)
    else:
        item_payload["resolvedAt"] = str(payload.get("timestamp") or envelope.timestamp)
        outcome = _nonempty_string(payload.get("outcome"))
        actor_ref = _nonempty_string(payload.get("actor_ref"))
        reason = _nonempty_string(payload.get("reason"))
        if outcome is not None:
            item_payload["outcome"] = outcome
        if "response" in payload:
            item_payload["response"] = payload["response"]
        if actor_ref is not None:
            item_payload["actor"] = actor_ref
        if reason is not None:
            item_payload["reason"] = reason

    is_approval = interaction_kind == "approval"
    return ConversationItem(
        item_id=interaction_id,
        source_event_ids=(str(envelope.event_id),),
        session_id=session_id,
        run_id=run_id,
        kind="approval" if is_approval else "progress",
        operation="append" if is_requested else "completed",
        lifecycle="pending" if is_requested else "completed",
        payload_schema_ref=(
            "conversation.item.approval/v1"
            if is_approval
            else "conversation.item.structured-input/v1"
        ),
        payload=item_payload,
        capability_ref="approval" if is_approval else "structured_input",
        native_ref={
            "protocol": "agent-kernel/interaction-v1",
            "eventId": str(envelope.event_id),
            "cursor": envelope.seq,
        },
    )


def _positive_revision(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _item_id(event: RuntimeEvent) -> str:
    if isinstance(
        event,
        (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted, ItemFailed),
    ):
        return event.item_id
    if isinstance(event, (InteractionRequested, InteractionResolved)):
        return event.interaction_id
    if isinstance(event, (ContinuationCreated, ContinuationResumed)):
        return event.continuation_id
    return event.event_id


def _text_from_update(event: ItemUpdated) -> str:
    return event.update.text if isinstance(event.update, TextContent) else ""


def _text_from_item_event(
    event: ItemStarted | ItemUpdated | ItemSnapshotReplaced | ItemCompleted,
) -> str:
    if isinstance(event, ItemUpdated):
        return _text_from_update(event)
    return _text_from_snapshot(_item_snapshot(event))


def _text_from_snapshot(snapshot: ContentSnapshot | None) -> str:
    if snapshot is None:
        return ""
    for part in snapshot.parts:
        if isinstance(part, TextContent):
            return part.text
    return ""


def _tool_payload(snapshot: ContentSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    call = next((item for item in snapshot.parts if isinstance(item, ToolCallContent)), None)
    result = next((item for item in snapshot.parts if isinstance(item, ToolResultContent)), None)
    payload: dict[str, Any] = {}
    if call is not None:
        payload.update({"callId": call.call_id, "tool": call.name, "args": call.arguments})
    if result is not None:
        # A Codex tool_result can arrive as a separate native item from the
        # corresponding tool_call. Keep the provider call identity in both
        # projections so a renderer can enrich the original card in place
        # rather than appending a duplicate result card.
        payload.update(
            {
                "callId": result.call_id,
                "output": result.result,
                "isError": result.is_error,
            }
        )
    return payload


def _item_snapshot(
    event: ItemStarted | ItemUpdated | ItemSnapshotReplaced | ItemCompleted,
) -> ContentSnapshot | None:
    if isinstance(event, ItemStarted):
        return event.initial
    if isinstance(event, ItemUpdated):
        return ContentSnapshot(parts=(event.update,))
    return event.snapshot


def _artifact_payload(snapshot: ContentSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    artifact = next(
        (part for part in snapshot.parts if isinstance(part, ArtifactContent)), None
    )
    if artifact is None:
        return {}
    return {
        "artifactId": artifact.artifact_id,
        "name": artifact.name,
        "mimeType": artifact.mime_type,
        "uri": artifact.uri,
    }


def _data_payload(snapshot: ContentSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    data = next((part for part in snapshot.parts if isinstance(part, DataContent)), None)
    return {"data": data.data} if data is not None else {}


def _goal_payload(snapshot: ContentSnapshot | None) -> dict[str, Any]:
    wrapped = _data_payload(snapshot)
    value = wrapped.get("data")
    return dict(value) if isinstance(value, Mapping) else wrapped


def _is_codex_plan_item(event: RuntimeEvent) -> bool:
    return (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "data"
        and event.source.framework == "codex"
        and (
            event.source.metadata.get("native_item_kind") == "plan"
            or event.source.metadata.get("method") == "turn/plan/updated"
        )
    )


def _is_codex_goal_item(event: RuntimeEvent) -> bool:
    return (
        isinstance(event, (ItemStarted, ItemUpdated, ItemSnapshotReplaced, ItemCompleted))
        and event.item_kind == "data"
        and event.source.framework == "codex"
        and event.source.metadata.get("method")
        in {"thread/goal/updated", "thread/goal/cleared"}
    )


def _native_ref(event: RuntimeEvent) -> dict[str, Any]:
    source = event.source
    return {
        key: value
        for key, value in {
            "framework": source.framework,
            "protocol": source.protocol,
            "eventId": source.native_event_id,
            "cursor": source.native_cursor,
            "runId": source.native_run_id,
            "itemId": source.native_item_id,
        }.items()
        if value is not None
    }


__all__ = ["project_conversation_item", "project_interaction_conversation_item"]
