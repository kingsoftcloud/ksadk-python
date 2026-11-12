import json

import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from pydantic import ValidationError

from ksadk.events.adapters.a2a import A2AEventAdapter
from tests.events.adapters.test_a2a import _context, _GetTaskClient, _update, _working


def restore(adapter, context):
    wire = json.loads(json.dumps(adapter.checkpoint(context)))
    return A2AEventAdapter.restore_checkpoint(wire)


def test_partial_artifact_checkpoint_preserves_append_identity_and_occurrences():
    original, context = A2AEventAdapter(), _context()
    original.map_event(_working(), context, timestamp=1.0)
    first = _update("hello ", event_id="first")
    original.map_event(first, context, timestamp=2.0)
    recovered, recovered_context = restore(original, context)
    assert recovered.map_event(first, recovered_context, timestamp=3.0) == ()
    last = _update("world", append=True, last_chunk=True, event_id="last")
    assert recovered.map_event(last, recovered_context, timestamp=4.0) == original.map_event(
        last, context, timestamp=4.0
    )
    assert recovered.checkpoint(recovered_context) == original.checkpoint(context)


@pytest.mark.asyncio
async def test_terminal_get_task_after_restore_never_duplicates_output():
    original, context = A2AEventAdapter(), _context()
    original.map_event(_working(), context, timestamp=1.0)
    task = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[Artifact(artifact_id="artifact-1", parts=[Part(text="answer")])],
        history=[
            Message(
                message_id="message-final",
                context_id="context-9",
                task_id="task-9",
                role=Role.ROLE_AGENT,
                parts=[Part(text="final")],
                metadata={"private": "private-native-metadata"},
            )
        ],
    )
    result = await original.reconcile(
        _GetTaskClient(task), context, reason="terminal", timestamp=2.0
    )
    assert result.consistent and result.events
    saved = original.checkpoint(context)
    assert "private-native-metadata" not in json.dumps(saved)
    recovered, recovered_context = restore(original, context)
    again = await recovered.reconcile(
        _GetTaskClient(task), recovered_context, reason="terminal", timestamp=3.0
    )
    assert again.consistent and again.events == ()
    changed = Task.FromString(task.SerializeToString())
    changed.artifacts[0].parts[0].text = "changed"
    conflict = await recovered.reconcile(
        _GetTaskClient(changed), recovered_context, reason="terminal", timestamp=4.0
    )
    assert not conflict.consistent


def test_checkpoint_rejects_unknown_versions_and_duplicate_parts():
    adapter, context = A2AEventAdapter(), _context()
    adapter.map_event(_update("hello"), context, timestamp=1.0)
    payload = adapter.checkpoint(context)
    with pytest.raises(ValidationError):
        A2AEventAdapter.restore_checkpoint({**payload, "version": 2})
    artifact = payload["artifacts"]["artifact-1"]
    artifact["snapshot"]["parts"] *= 2
    with pytest.raises(ValidationError, match="DUPLICATE_PART"):
        A2AEventAdapter.restore_checkpoint(payload)


def test_input_required_checkpoint_preserves_continuation_identity():
    adapter, context = A2AEventAdapter(), _context()
    requested = TaskStatusUpdateEvent(
        task_id="task-9",
        context_id="context-9",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="input-7",
                task_id="task-9",
                context_id="context-9",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Choose a region")],
                metadata={"input_schema": {"type": "string"}},
            ),
        ),
        metadata={"event_id": "input-required-1"},
    )
    adapter.map_event(requested, context, timestamp=1.0)
    assert "active_interaction_kind" not in adapter.checkpoint(context)
    recovered, recovered_context = restore(adapter, context)
    assert recovered.map_event(requested, recovered_context, timestamp=2.0) == ()
    assert recovered.map_event(_working(), recovered_context, timestamp=3.0) == adapter.map_event(
        _working(), context, timestamp=3.0
    )
