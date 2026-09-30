from __future__ import annotations

from ksadk.events.canonical import ContinuationCreated, ContinuationResumed, SourceRef
from ksadk.events.canonical_store import runtime_event_to_session_event
from ksadk.server.routes.projection import (
    _apply_checkpoint_resume_audit,
    _checkpoint_event_to_action_payload,
    _record_resume_audit,
)


def _continuation_created() -> ContinuationCreated:
    return ContinuationCreated(
        schema_version=2,
        event_id="event-checkpoint-created",
        seq=1,
        timestamp=100.0,
        run_id="run-1",
        run_seq=1,
        scope_id="run:run-1",
        source=SourceRef(
            framework="langgraph",
            metadata={"backend": "postgres", "scope": "thread", "durable": True},
        ),
        continuation_id="checkpoint-1",
        continuation_kind="graph_checkpoint",
        resumable=True,
        ref={"checkpoint_id": "checkpoint-1", "checkpoint_ns": ""},
    )


def _continuation_resumed() -> ContinuationResumed:
    return ContinuationResumed(
        schema_version=2,
        event_id="event-checkpoint-resumed",
        seq=2,
        timestamp=101.0,
        run_id="run-1",
        run_seq=2,
        scope_id="run:run-1",
        source=SourceRef(framework="langgraph"),
        continuation_id="checkpoint-1",
        continuation_kind="graph_checkpoint",
        resume_attempt_id="resume-attempt-1",
    )


def test_canonical_continuation_resumed_updates_checkpoint_audit() -> None:
    created = runtime_event_to_session_event("session-1", _continuation_created())
    resumed = runtime_event_to_session_event("session-1", _continuation_resumed())
    audit: dict[str, dict[tuple[str, str], dict[str, object]]] = {}

    _record_resume_audit(audit, resumed)

    checkpoint = _checkpoint_event_to_action_payload(created)
    assert checkpoint is not None
    checkpoint = _apply_checkpoint_resume_audit(checkpoint, audit["session-1"])

    assert checkpoint["ResumeCount"] == 1
    assert checkpoint["LastResumedAt"] == 101.0
    assert checkpoint["CheckpointStatus"] == "resumed"
