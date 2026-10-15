"""Typed private checkpoint codec; no pickles, transport objects or raw protobufs."""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.events.adapters._a2a_support import A2AAdapterContext, _ArtifactState, _MessageState
from ksadk.events.content import ContentSnapshot
from ksadk.events.identity import stable_item_id


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _Artifact(_Contract):
    artifact_id: str
    item_id: str
    snapshot: ContentSnapshot
    closed: bool
    present: bool

    @model_validator(mode="after")
    def unique_parts(self):
        ids = [part.part_id for part in self.snapshot.parts]
        if len(ids) != len(set(ids)):
            raise ValueError("A2A_CHECKPOINT_DUPLICATE_PART")
        return self


class _Context(_Contract):
    run_id: str
    context_id: str
    task_id: str | None
    next_seq: int = Field(ge=0)
    fixed_scope_id: str | None
    parent_scope_id: str | None
    direct_message_id: str | None


class AdapterCheckpoint(_Contract):
    version: Literal[1]
    context: _Context
    artifacts: dict[str, _Artifact]
    messages: dict[str, str]
    tool_activities: dict[str, tuple[str, bool]]
    tool_activity_bytes: int = Field(ge=0)
    seen_occurrences: list[tuple[str, str]]
    provisional_ordinals: dict[str, int]
    interaction_payloads: list[tuple[int, str, str]]
    run_started: bool
    run_interrupted: bool
    active_interaction: tuple[str, str] | None
    active_interaction_kind: Literal["structured_input", "approval"] = "structured_input"
    terminal_snapshot_fingerprint: str | None


def export_checkpoint(adapter, context):
    checkpoint = AdapterCheckpoint(
        version=1,
        context=_Context(
            run_id=context.run_id,
            context_id=context.context_id,
            task_id=context.task_id,
            next_seq=context.peek_placeholder_seq(),
            fixed_scope_id=context.fixed_scope_id,
            parent_scope_id=context.parent_scope_id,
            direct_message_id=context._direct_message_id,
        ),
        artifacts={
            key: _Artifact(
                artifact_id=value.artifact_id,
                item_id=value.item_id,
                snapshot=value.snapshot(),
                closed=value.closed,
                present=value.present,
            )
            for key, value in adapter._artifacts.items()
        },
        messages={key: value.signature for key, value in adapter._messages.items()},
        tool_activities=adapter._tool_activities,
        tool_activity_bytes=adapter._tool_activity_bytes,
        seen_occurrences=list(adapter._seen_occurrences.items()),
        provisional_ordinals=adapter._provisional_ordinals,
        interaction_payloads=[
            (key[0], key[1], value) for key, value in adapter._interaction_payloads.items()
        ],
        run_started=adapter._run_started,
        run_interrupted=adapter._run_interrupted,
        active_interaction=adapter._active_interaction,
        active_interaction_kind=adapter._active_interaction_kind,
        terminal_snapshot_fingerprint=adapter._terminal_snapshot_fingerprint,
    )
    payload = checkpoint.model_dump(mode="json")
    # Keep the established checkpoint wire shape for legacy input flows.
    # Approval checkpoints require a reader that understands their semantics.
    if adapter._active_interaction_kind == "structured_input":
        payload.pop("active_interaction_kind")
    return payload


def restore_checkpoint(adapter_type, payload):
    import json

    # JSON validation allows the wire representation of tuples while retaining
    # strict checks for scalars and discriminated canonical content.
    checkpoint = AdapterCheckpoint.model_validate_json(json.dumps(payload, allow_nan=False))
    value = checkpoint.context
    context = A2AAdapterContext(
        run_id=value.run_id,
        context_id=value.context_id,
        task_id=value.task_id,
        initial_seq=value.next_seq,
        fixed_scope_id=value.fixed_scope_id,
        parent_scope_id=value.parent_scope_id,
    )
    if value.direct_message_id is not None:
        context.bind_direct_message(value.direct_message_id)
    adapter = adapter_type()
    for key, artifact in checkpoint.artifacts.items():
        if key != artifact.artifact_id or artifact.item_id != stable_item_id(
            "a2a", *context.item_identity, "artifact", artifact.artifact_id
        ):
            raise ValueError("A2A_CHECKPOINT_ARTIFACT_IDENTITY_CONFLICT")
        adapter._artifacts[key] = _ArtifactState(
            artifact_id=artifact.artifact_id,
            item_id=artifact.item_id,
            parts={p.part_id: p for p in artifact.snapshot.parts},
            part_order=[p.part_id for p in artifact.snapshot.parts],
            closed=artifact.closed,
            present=artifact.present,
        )
    adapter._messages = {
        key: _MessageState(signature=value) for key, value in checkpoint.messages.items()
    }
    adapter._tool_activities = checkpoint.tool_activities
    adapter._tool_activity_bytes = checkpoint.tool_activity_bytes
    adapter._seen_occurrences = OrderedDict(checkpoint.seen_occurrences)
    adapter._provisional_ordinals = checkpoint.provisional_ordinals
    adapter._interaction_payloads = {
        (state, key): value for state, key, value in checkpoint.interaction_payloads
    }
    adapter._run_started = checkpoint.run_started
    adapter._run_interrupted = checkpoint.run_interrupted
    adapter._active_interaction = checkpoint.active_interaction
    adapter._active_interaction_kind = checkpoint.active_interaction_kind
    adapter._terminal_snapshot_fingerprint = checkpoint.terminal_snapshot_fingerprint
    return adapter, context
