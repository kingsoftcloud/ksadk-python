"""Conversation surface projection stays additive to established Studio events."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from ksadk.conversations.contracts import (
    ConversationAttachmentPart,
    ConversationCapability,
    ConversationInput,
    ConversationItem,
    ConversationSurface,
    ConversationTextPart,
    validate_conversation_input,
    validate_surface_input,
)
from ksadk.conversations.projector import (
    project_conversation_item,
    project_interaction_conversation_item,
)
from ksadk.conversations.reducer import ConversationItemReducer
from ksadk.events.canonical import (
    ApprovalRequest,
    InteractionRequested,
    ItemCompleted,
    ItemUpdated,
    RunCompleted,
    RuntimeEvent,
    SourceRef,
    UnknownCanonicalEvent,
    parse_runtime_event,
)
from ksadk.events.canonical_store import runtime_event_envelope
from ksadk.events.content import ContentSnapshot, DataContent, TextContent
from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.studio.run_service import _studio_envelope_projection, project_runtime_event

_GOLDEN_PATH = (
    Path(__file__).resolve().parents[1] / "events" / "fixtures" / "runtime_projection_golden.json"
)
_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "conversation" / "v1"


def _golden_events() -> list[RuntimeEvent]:
    raw = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    return [parse_runtime_event(event) for event in raw["events"]]


def _interaction_envelope(
    *,
    event_id: str,
    seq: int,
    event_type: str,
    revision: object = 1,
) -> SessionEventEnvelope:
    terminal = event_type != "interaction.requested"
    payload: dict[str, object] = {
        "schema_version": 1,
        "event_type": event_type,
        "interaction_id": "approval-1",
        "tenant_id": "tenant-1",
        "agent_instance_id": "agent-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "kind": "approval",
        "revision": revision,
        "timestamp": "2026-08-28T00:00:00Z",
    }
    if terminal:
        payload.update(
            {
                "outcome": "approved",
                "response": {"approved": True},
                "actor_ref": "account:user-1",
            }
        )
    else:
        payload["request"] = {
            "kind": "approval",
            "request_schema": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
            },
            "expires_at": "2026-08-28T00:05:00Z",
            "presentation": {
                "title": "run_command",
                "description": "Allow this command?",
            },
        }
    return SessionEventEnvelope(
        event_id=UUID(event_id),
        session_id="session-1",
        seq=seq,
        timestamp="2026-08-28T00:00:00Z",
        family="interaction",
        family_version=1,
        event_type=event_type,
        payload=payload,
        run_id="run-1",
        actor_ref="agent-kernel",
    )


def test_frozen_conversation_contract_fixtures_stay_valid() -> None:
    ConversationSurface.model_validate_json(
        (_CONTRACT_PATH / "fixtures" / "conversation-surface.json").read_text(encoding="utf-8")
    )
    ConversationItem.model_validate_json(
        (_CONTRACT_PATH / "fixtures" / "conversation-item.json").read_text(encoding="utf-8")
    )
    ConversationInput.model_validate_json(
        (_CONTRACT_PATH / "fixtures" / "conversation-input.json").read_text(encoding="utf-8")
    )


def test_surface_allows_matching_input_and_output_capabilities() -> None:
    surface = ConversationSurface(
        surface_id="studio",
        session_id="session-1",
        provider_ref="codex",
        inputs=(ConversationCapability(name="text", mode="native"),),
        outputs=(ConversationCapability(name="text", mode="native"),),
    )

    validate_surface_input(surface, {"text": "hello", "extensions": {"trace": True}})
    with pytest.raises(ValueError, match="not declared"):
        validate_surface_input(surface, {"approval": "approve"})


def test_input_requires_only_the_surface_capabilities_it_uses() -> None:
    surface = ConversationSurface(
        surface_id="studio",
        session_id="session-1",
        provider_ref="codex",
        inputs=(
            ConversationCapability(name="text", mode="native"),
            ConversationCapability(name="attachment.image", mode="translated"),
            ConversationCapability(name="model.select", mode="native"),
        ),
    )
    conversation_input = ConversationInput(
        input_id="input-1",
        session_id="session-1",
        idempotency_key="turn-1",
        parts=(
            ConversationTextPart(text="describe this image"),
            ConversationAttachmentPart(
                attachment_ref="attachment://image-1",
                media_type="image/png",
            ),
        ),
        model_ref="model:example",
    )

    validate_conversation_input(surface, conversation_input)
    with pytest.raises(ValueError, match="reasoning.effort"):
        validate_conversation_input(
            surface,
            conversation_input.model_copy(update={"reasoning": "high"}),
        )
    with pytest.raises(ValueError, match="approval"):
        validate_conversation_input(
            surface,
            conversation_input.model_copy(update={"extensions": {"ksadk.approval": "risk"}}),
        )
    with pytest.raises(ValueError, match="plan"):
        validate_conversation_input(
            surface,
            conversation_input.model_copy(update={"extensions": {"ksadk.collaboration": "plan"}}),
        )
    with pytest.raises(ValueError, match="goal"):
        validate_conversation_input(
            surface,
            conversation_input.model_copy(update={"extensions": {"ksadk.goal": "ship it"}}),
        )


def test_projection_preserves_identical_text_when_item_ids_differ() -> None:
    reducer = ConversationItemReducer()
    updates = [
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_kind == "message"
    ]
    assert [event.item_id for event in updates] == ["msg-legal-1", "msg-legal-2"]

    for event in updates:
        assert reducer.apply(project_conversation_item(event, session_id="session-1"))

    messages = reducer.items()
    assert [item.item_id for item in messages] == ["msg-legal-1", "msg-legal-2"]
    assert [item.payload["text"] for item in messages] == [
        "The answer is 42.",
        "The answer is 42.",
    ]


def test_codex_native_plan_and_goal_keep_typed_conversation_items() -> None:
    plan = ItemUpdated(
        schema_version=2,
        event_id="plan-delta-1",
        seq=1,
        timestamp=1.0,
        run_id="turn-1",
        scope_id="scope-1",
        source=SourceRef(
            framework="codex",
            native_item_id="plan-1",
            metadata={
                "method": "item/plan/delta",
                "native_item_kind": "plan",
            },
        ),
        item_id="plan-1",
        item_kind="data",
        op="append",
        update=TextContent(part_id="plan-text", text="1. inspect"),
    )
    goal = ItemCompleted(
        schema_version=2,
        event_id="goal-updated-1",
        seq=2,
        timestamp=2.0,
        run_id="turn-1",
        scope_id="scope-1",
        source=SourceRef(
            framework="codex",
            native_item_id="thread/goal/updated:2",
            metadata={
                "method": "thread/goal/updated",
                "native_item_kind": "notification",
            },
        ),
        item_id="goal-1",
        item_kind="data",
        snapshot=ContentSnapshot(
            parts=(
                DataContent(
                    part_id="goal-data",
                    data={"objective": "ship Release", "status": "active"},
                ),
            )
        ),
    )

    projected_plan = project_conversation_item(plan, session_id="session-1")
    projected_goal = project_conversation_item(goal, session_id="session-1")

    assert projected_plan.kind == "plan"
    assert projected_plan.capability_ref == "plan"
    assert projected_plan.payload == {"text": "1. inspect"}
    assert projected_plan.native_ref["itemId"] == "plan-1"
    assert projected_goal.kind == "goal"
    assert projected_goal.operation == "completed"
    assert projected_goal.capability_ref == "goal"
    assert projected_goal.payload == {
        "objective": "ship Release",
        "status": "active",
    }


def test_codex_user_message_is_transcript_and_only_run_event_is_terminal() -> None:
    user = ItemCompleted(
        schema_version=2,
        event_id="user-message-completed",
        seq=1,
        timestamp=1.0,
        run_id="turn-1",
        scope_id="scope-1",
        source=SourceRef(
            framework="codex",
            native_item_id="user-1",
            metadata={"native_item_kind": "userMessage"},
        ),
        item_id="user-1",
        item_kind="data",
        snapshot=ContentSnapshot(
            parts=(
                DataContent(
                    part_id="user-data",
                    data={
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "继续查询"}],
                    },
                ),
            )
        ),
    )
    run_completed = RunCompleted(
        schema_version=2,
        event_id="run-completed",
        seq=2,
        timestamp=2.0,
        run_id="turn-1",
        scope_id="scope-1",
        source=SourceRef(framework="codex"),
        status="completed",
        output_refs=(),
    )

    user_item = project_conversation_item(user, session_id="session-1")
    terminal_item = project_conversation_item(run_completed, session_id="session-1")

    assert user_item.kind == "user_message"
    assert user_item.payload == {"text": "继续查询"}
    assert user_item.lifecycle == "completed"
    assert terminal_item.kind == "progress"
    assert terminal_item.payload == {"status": "completed"}


def test_reconnect_replays_event_idempotently_but_appends_new_delta() -> None:
    event = next(
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_id == "msg-legal-1"
    )
    reducer = ConversationItemReducer()
    item = project_conversation_item(event, session_id="session-1")

    assert reducer.apply(item)
    assert not reducer.apply(item)

    next_item = item.model_copy(
        update={
            "source_event_ids": ("evt-next-delta",),
            "payload": {"text": " Again."},
        }
    )
    assert reducer.apply(next_item)
    assert reducer.items()[0].payload["text"] == "The answer is 42. Again."


def test_reconnect_cannot_regress_a_terminal_item_with_an_older_delta() -> None:
    event = next(
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_id == "msg-legal-1"
    )
    reducer = ConversationItemReducer()
    streaming = project_conversation_item(event, session_id="session-1")
    completed = streaming.model_copy(
        update={
            "source_event_ids": ("evt-terminal",),
            "operation": "completed",
            "lifecycle": "completed",
            "payload": {"text": "The answer is 42."},
        }
    )

    assert reducer.apply(completed)
    assert not reducer.apply(streaming)
    assert reducer.items()[0].lifecycle == "completed"
    assert reducer.items()[0].payload["text"] == "The answer is 42."


def test_studio_projection_keeps_legacy_shape_and_adds_typed_item() -> None:
    event = next(
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_id == "msg-legal-1"
    )

    event_type, payload = project_runtime_event(event, session_id="real-session")

    assert event_type == "message.delta"
    assert payload["text"] == "The answer is 42."
    assert payload["itemId"] == "msg-legal-1"
    assert payload["conversationItem"] == {
        "apiVersion": "conversation.ksadk.io/v1",
        "kindVersion": 1,
        "itemId": "msg-legal-1",
        "sourceEventIds": ["evt-msg-1-delta-1"],
        "sessionId": "real-session",
        "runId": "golden-run",
        "kind": "assistant_text",
        "operation": "append",
        "lifecycle": "streaming",
        "visibility": "public",
        "payloadSchemaRef": "conversation.item.assistant_text/v1",
        "payload": {"text": "The answer is 42."},
        "nativeRef": {"framework": "ksadk"},
    }


def test_studio_projection_uses_the_durable_public_run_for_replay() -> None:
    event = next(
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_id == "msg-legal-1"
    )

    _, payload = project_runtime_event(
        event,
        session_id="real-session",
        public_run_id="run-studio-durable",
    )

    assert payload["runId"] == "run-studio-durable"
    assert payload["runtimeRunId"] == "golden-run"
    assert payload["conversationItem"]["runId"] == "run-studio-durable"
    assert payload["runtimeEvent"]["run_id"] == "golden-run"


def test_kernel_envelope_uses_the_same_additive_conversation_projection() -> None:
    event = next(
        event
        for event in _golden_events()
        if isinstance(event, ItemUpdated) and event.item_id == "msg-legal-1"
    )
    envelope = runtime_event_envelope("real-session", event)

    projected = _studio_envelope_projection(envelope)

    assert projected is not None
    event_type, payload = projected
    assert event_type == "message.delta"
    assert payload["text"] == "The answer is 42."
    assert payload["conversationItem"]["itemId"] == "msg-legal-1"
    assert payload["conversationItem"]["sessionId"] == "real-session"


def test_interaction_envelope_projects_authoritative_writable_revision() -> None:
    envelope = _interaction_envelope(
        event_id="00000000-0000-0000-0000-000000000101",
        seq=101,
        event_type="interaction.requested",
    )

    projected = _studio_envelope_projection(envelope)

    assert projected is not None
    event_type, data = projected
    assert event_type == "approval.requested"
    assert data["revision"] == 1
    assert data["approvalId"] == "approval-1"
    item = ConversationItem.model_validate(data["conversationItem"])
    assert item.item_id == "approval-1"
    assert item.operation == "append"
    assert item.lifecycle == "pending"
    assert item.payload == {
        "interactionId": "approval-1",
        "interactionKind": "approval",
        "revision": 1,
        "kind": "run_command",
        "inputSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
        },
        "createdAt": "2026-08-28T00:00:00Z",
        "title": "run_command",
        "detail": "Allow this command?",
        "prompt": "Allow this command?",
        "expiresAt": "2026-08-28T00:05:00Z",
        "presentation": {
            "title": "run_command",
            "description": "Allow this command?",
        },
    }
    assert item.native_ref == {
        "protocol": "agent-kernel/interaction-v1",
        "eventId": "00000000-0000-0000-0000-000000000101",
        "cursor": 101,
    }


def test_interaction_resolved_is_terminal_and_cannot_regress_on_replay() -> None:
    requested = project_interaction_conversation_item(
        _interaction_envelope(
            event_id="00000000-0000-0000-0000-000000000101",
            seq=101,
            event_type="interaction.requested",
        )
    )
    resolved_envelope = _interaction_envelope(
        event_id="00000000-0000-0000-0000-000000000102",
        seq=102,
        event_type="interaction.resolved",
        revision=2,
    )
    projected = _studio_envelope_projection(resolved_envelope)

    assert requested is not None
    assert projected is not None
    event_type, data = projected
    assert event_type == "approval.resolved"
    assert data["decision"] == "approved"
    resolved = ConversationItem.model_validate(data["conversationItem"])
    assert resolved.operation == "completed"
    assert resolved.lifecycle == "completed"
    assert resolved.payload["revision"] == 2
    assert resolved.payload["outcome"] == "approved"

    reducer = ConversationItemReducer()
    assert reducer.apply(requested)
    assert reducer.apply(resolved)
    assert not reducer.apply(requested)
    assert reducer.items()[0].lifecycle == "completed"
    assert reducer.items()[0].payload["revision"] == 2


def test_durable_interaction_fact_upgrades_runtime_item_without_duplicate_card() -> None:
    runtime_requested = InteractionRequested(
        schema_version=2,
        event_id="runtime-approval-requested",
        seq=1,
        timestamp=1.0,
        run_id="run-1",
        scope_id="run:run-1",
        source=SourceRef(framework="codex"),
        interaction_id="approval-1",
        interaction_kind="approval",
        request=ApprovalRequest(
            call_id="call-1",
            kind="command",
            detail={"command": "echo safe"},
        ),
    )
    runtime_pending = project_conversation_item(
        runtime_requested,
        session_id="session-1",
    )
    durable_pending = project_interaction_conversation_item(
        _interaction_envelope(
            event_id="00000000-0000-0000-0000-000000000101",
            seq=101,
            event_type="interaction.requested",
        )
    )
    durable_terminal = project_interaction_conversation_item(
        _interaction_envelope(
            event_id="00000000-0000-0000-0000-000000000102",
            seq=102,
            event_type="interaction.resolved",
            revision=2,
        )
    )
    assert "revision" not in runtime_pending.payload
    assert runtime_pending.payload["callId"] == "call-1"
    assert durable_pending is not None
    assert durable_terminal is not None

    reducer = ConversationItemReducer()
    assert reducer.apply(runtime_pending)
    assert reducer.apply(durable_pending)
    assert len(reducer.items()) == 1
    assert reducer.items()[0].item_id == "approval-1"
    assert reducer.items()[0].payload["revision"] == 1
    assert set(reducer.items()[0].source_event_ids) == {
        "runtime-approval-requested",
        "00000000-0000-0000-0000-000000000101",
    }

    assert reducer.apply(durable_terminal)
    runtime_replay = runtime_pending.model_copy(
        update={"source_event_ids": ("runtime-approval-replayed",)}
    )
    durable_replay = durable_pending.model_copy(
        update={"source_event_ids": ("durable-approval-replayed",)}
    )
    assert not reducer.apply(runtime_replay)
    assert not reducer.apply(durable_replay)
    assert len(reducer.items()) == 1
    assert reducer.items()[0].lifecycle == "completed"
    assert reducer.items()[0].payload["revision"] == 2


@pytest.mark.parametrize("revision", [None, 0, -1, True, "1"])
def test_interaction_envelope_never_invents_a_writable_revision(revision: object) -> None:
    envelope = _interaction_envelope(
        event_id="00000000-0000-0000-0000-000000000103",
        seq=103,
        event_type="interaction.requested",
        revision=revision,
    )

    projected = _studio_envelope_projection(envelope)

    assert projected is not None
    event_type, data = projected
    assert event_type == "interaction.requested"
    assert data["interactionReadOnly"] is True
    assert "conversationItem" not in data
    assert "approvalId" not in data


@pytest.mark.asyncio
async def test_kernel_subscription_delivers_interaction_family_to_studio_projector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ksadk.kernel import ingress

    requested = _interaction_envelope(
        event_id="00000000-0000-0000-0000-000000000104",
        seq=104,
        event_type="interaction.requested",
    )
    resolved = _interaction_envelope(
        event_id="00000000-0000-0000-0000-000000000105",
        seq=105,
        event_type="interaction.resolved",
        revision=2,
    )

    permit = SimpleNamespace(permit_id="permit-1")

    class _Kernel:
        async def subscribe(self, _subscription, *, permit):
            assert permit is trusted.permit
            yield requested
            yield resolved

    monkeypatch.setattr(ingress, "get_agent_kernel", lambda: _Kernel())
    trusted = SimpleNamespace(
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        permit=permit,
    )

    observed = [
        (seq, projected)
        async for seq, projected in ingress.subscribe_projected(
            "session-1",
            trusted=trusted,
            projector=_studio_envelope_projection,
        )
    ]

    assert [seq for seq, _projected in observed] == [104, 105]
    assert [projected[0] for _seq, projected in observed] == [
        "approval.requested",
        "approval.resolved",
    ]
    assert [
        projected[1]["conversationItem"]["payload"]["revision"] for _seq, projected in observed
    ] == [1, 2]


def test_unhandled_event_type_projects_as_hidden_to_prevent_fallback_spam() -> None:
    """Additive provider events degrade silently instead of spamming fallback cards."""
    unknown = UnknownCanonicalEvent(
        schema_version=2,
        event_id="future-event-1",
        seq=1,
        timestamp=1.0,
        run_id="turn-future",
        scope_id="scope-1",
        source=SourceRef(framework="codex"),
        event_type="future.quantum.observed",
        payload={"state": "superposed"},
    )
    projected = project_conversation_item(unknown, session_id="session-1")  # type: ignore[arg-type]
    assert projected.kind == "unknown"
    assert projected.visibility == "hidden"
    assert projected.payload_schema_ref == "conversation.item.unknown/v1"


def test_explicit_public_unknown_event_projects_as_safe_fallback_card() -> None:
    unknown = UnknownCanonicalEvent(
        schema_version=2,
        event_id="future-public-event-1",
        seq=1,
        timestamp=1.0,
        run_id="turn-future",
        scope_id="scope-1",
        source=SourceRef(
            framework="codex",
            metadata={"conversation_visibility": "public"},
        ),
        event_type="future.public.widget",
        payload={"unsafe": "provider payload must not reach the renderer"},
    )
    projected = project_conversation_item(unknown, session_id="session-1")  # type: ignore[arg-type]
    assert projected.kind == "unknown"
    assert projected.visibility == "public"
    assert projected.payload == {
        "eventType": "future.public.widget",
        "summary": "This content requires a newer renderer.",
    }
