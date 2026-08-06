"""RuntimeEvent external ingestion conformance regression tests (goal 19)."""

from __future__ import annotations

import json
from typing import Any, Callable, cast

import pytest
from a2a.types import TaskState, TaskStatus

from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.events.replay import replay_transcript
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import (
    RuntimeEventStore,
    runtime_event_to_session_event,
    session_event_to_runtime_event,
)


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
    with pytest.raises(ValueError, match="payload.*缺必填键"):
        load(
            _event_data(
                event_type=EventType.TOOL_CALL_BEGIN,
                phase=None,
                payload={"name": "search"},
            )
        )


def test_store_write_boundary_rejects_unchecked_runtime_event():
    unchecked = RuntimeEvent(**_event_data(event_type="runtime.unknown"))

    with pytest.raises(ValueError, match="unknown event_type"):
        runtime_event_to_session_event(unchecked)


def test_store_read_boundary_rejects_tampered_persisted_event():
    valid = RuntimeEvent.from_dict(_event_data())
    persisted = runtime_event_to_session_event(valid)
    persisted.event_type = EventType.TOOL_CALL_BEGIN
    persisted.content = {"phase": None, "payload": {"name": "search"}}

    with pytest.raises(ValueError, match="payload.*缺必填键"):
        session_event_to_runtime_event(persisted)


@pytest.mark.asyncio
async def test_replay_boundary_rejects_unchecked_store_event():
    class UncheckedStore:
        async def list(self, *args: Any, **kwargs: Any) -> list[RuntimeEvent]:
            return [RuntimeEvent(**_event_data(event_type="runtime.unknown"))]

    with pytest.raises(ValueError, match="unknown event_type"):
        await replay_transcript(cast(RuntimeEventStore, UncheckedStore()), "session-1")


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

    event.validate_conformance()
    assert event.event_type == EventType.RUN_FAILED
    assert event.payload["error"]


def test_a2a_output_boundary_rejects_unchecked_runtime_event():
    unchecked = RuntimeEvent(**_event_data(payload={}))

    with pytest.raises(ValueError, match="payload.*缺必填键"):
        A2AEventAdapter.event_to_text_part(unchecked)
