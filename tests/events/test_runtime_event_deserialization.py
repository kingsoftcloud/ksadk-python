"""RuntimeEvent external ingestion conformance regression tests (goal 19).

v1 wire deserialization boundaries are tested through ``v1_compat`` (the only
owner of the legacy v1 envelope).  A2A ingestion and store boundaries use the
canonical schema-v2 types directly.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest
from a2a.types import TaskState, TaskStatus

from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.events.canonical import RunFailed, RunStarted, SourceRef
from ksadk.events.store import (
    runtime_event_to_session_event,
    session_event_to_runtime_event,
)
from ksadk.events.v1_compat import EventTypeV1 as EventType, RuntimeEventV1 as RuntimeEvent


def _event_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "event_id": "evt_external_1",
        "event_type": EventType.TEXT_DELTA,
        "timestamp": 1.0,
        "agent_id": "agent-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "invocation_id": "invocation-1",
        "seq_id": 1,
        "phase": "commentary",
        "payload": {"text": "hello"},
    }
    data.update(overrides)
    return data


LOADERS: tuple[Callable[[dict[str, Any]], RuntimeEvent], ...] = (
    RuntimeEvent.from_dict,
    lambda data: RuntimeEvent.from_json(json.dumps(data)),
)


@pytest.mark.parametrize("load", LOADERS, ids=("from_dict", "from_json"))
def test_external_deserialization_rejects_unknown_event_type(load):
    with pytest.raises(ValueError, match="unknown event_type"):
        load(_event_data(event_type="runtime.unknown"))


@pytest.mark.parametrize("load", LOADERS, ids=("from_dict", "from_json"))
def test_external_deserialization_rejects_phase_on_non_text_event(load):
    with pytest.raises(ValueError, match="phase"):
        load(
            _event_data(
                event_type=EventType.RUN_STARTED,
                phase="commentary",
                payload={"status": "in_progress"},
            )
        )


@pytest.mark.parametrize("load", LOADERS, ids=("from_dict", "from_json"))
def test_external_deserialization_rejects_missing_required_payload_key(load):
    with pytest.raises(ValueError, match="missing required keys"):
        load(
            _event_data(
                event_type=EventType.TOOL_CALL_BEGIN,
                phase=None,
                payload={"name": "search"},
            )
        )


def test_store_write_boundary_rejects_unchecked_runtime_event():
    unchecked = RuntimeEvent(**_event_data(event_type="runtime.unknown"))

    with pytest.raises(ValueError, match="schema_version=2 only"):
        runtime_event_to_session_event("session-1", unchecked)


def test_store_read_boundary_rejects_tampered_persisted_event():
    valid = RunStarted(
        schema_version=2,
        event_id="evt-1",
        seq=0,
        timestamp=1.0,
        run_id="run-1",
        scope_id="scope-1",
        source=SourceRef(framework="adk"),
        status="running",
    )
    persisted = runtime_event_to_session_event("session-1", valid)
    persisted.event_type = "item.updated"  # tamper: envelope no longer matches content

    with pytest.raises(ValueError, match="envelope does not match"):
        session_event_to_runtime_event(persisted)


@pytest.mark.parametrize(
    "state",
    (TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED),
    ids=("failed", "rejected"),
)
def test_a2a_failed_status_ingestion_produces_conformant_runtime_event(state):
    event = A2AEventAdapter().task_status_to_event(
        TaskStatus(state=state),
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="invocation-1",
        seq_id=1,
    )

    assert isinstance(event, RunFailed)
    assert event.error.message
