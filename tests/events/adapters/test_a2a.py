"""A2A SDK 1.1.0 protobuf events to canonical RuntimeEvent tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from a2a.types import (
    Artifact,
    GetTaskRequest,
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from ksadk.events.adapters.a2a import (
    A2AAdapterContext,
    A2AEventAdapter,
    A2AMappingError,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
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
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.content import (
    ArtifactContent,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.reducer import StreamReducer


def _context(*, initial_seq: int = 10) -> A2AAdapterContext:
    return A2AAdapterContext(
        run_id="runtime-run-9",
        context_id="context-9",
        task_id="task-9",
        initial_seq=initial_seq,
    )


def _value(value: Any) -> Value:
    return ParseDict(value, Value())


def _update(
    text: str,
    *,
    artifact_id: str = "artifact-1",
    append: bool = False,
    last_chunk: bool = False,
    event_id: str | None = None,
    cursor: str | None = None,
) -> TaskArtifactUpdateEvent:
    metadata: dict[str, Any] = {}
    if event_id is not None:
        metadata["event_id"] = event_id
    if cursor is not None:
        metadata["seq"] = cursor
    return TaskArtifactUpdateEvent(
        task_id="task-9",
        context_id="context-9",
        artifact=Artifact(
            artifact_id=artifact_id,
            name="response",
            parts=[Part(text=text)],
        ),
        append=append,
        last_chunk=last_chunk,
        metadata=metadata,
    )


def _working() -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        metadata={"event_id": "status-working", "seq": "1"},
    )


def _terminal(state: int = TaskState.TASK_STATE_COMPLETED) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id="task-9",
        context_id="context-9",
        status=TaskStatus(state=state),
        metadata={"event_id": f"status-{TaskState.Name(state)}", "seq": "99"},
    )


def _map(
    adapter: A2AEventAdapter,
    native: Any,
    context: A2AAdapterContext,
    *,
    timestamp: float = 1000.0,
) -> tuple[RuntimeEvent, ...]:
    return adapter.map_event(native, context, timestamp=timestamp)


def _reduce(events: Iterable[RuntimeEvent]) -> StreamReducer:
    reducer = StreamReducer()
    for event in events:
        reducer.apply(event)
    return reducer


class _GetTaskClient:
    def __init__(self, result: Task | Exception) -> None:
        self.result = result
        self.requests: list[GetTaskRequest] = []

    async def get_task(self, request: GetTaskRequest, **_: Any) -> Task:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_real_a2a_artifact_ids_preserve_equal_text_as_two_items() -> None:
    """Break caught: equal artifact text is merged by body instead of artifact_id."""

    adapter = A2AEventAdapter()
    context = _context()
    events = [*_map(adapter, _working(), context)]
    events.extend(
        _map(
            adapter,
            _update(
                "same",
                artifact_id="artifact-a",
                last_chunk=True,
                event_id="artifact-a-final",
            ),
            context,
        )
    )
    events.extend(
        _map(
            adapter,
            _update(
                "same",
                artifact_id="artifact-b",
                last_chunk=True,
                event_id="artifact-b-final",
            ),
            context,
        )
    )

    replaced = [event for event in events if isinstance(event, ItemSnapshotReplaced)]
    assert len(replaced) == 2
    assert len({event.item_id for event in replaced}) == 2
    assert [event.snapshot.parts[0].text for event in replaced] == ["same", "same"]
    assert {event.source.native_item_id for event in replaced} == {
        "artifact-a",
        "artifact-b",
    }
    assert len({event.scope_id for event in replaced}) == 1
    assert all(event.schema_version == 2 and event.source.framework == "a2a" for event in events)


def test_append_and_replace_are_independent_content_operations() -> None:
    """Break caught: SDK append parts are concatenated into one text payload."""

    adapter = A2AEventAdapter()
    context = _context()
    events = [*_map(adapter, _working(), context)]
    for native in (
        _update("hel", event_id="artifact-1", cursor="10"),
        _update("lo", append=True, event_id="artifact-2", cursor="11"),
        _update("final", append=False, event_id="artifact-3", cursor="12"),
        _update("!", append=True, last_chunk=True, event_id="artifact-4", cursor="13"),
    ):
        events.extend(_map(adapter, native, context))

    replacements = [event for event in events if isinstance(event, ItemSnapshotReplaced)]
    assert [[part.text for part in event.snapshot.parts] for event in replacements] == [
        ["hel"],
        ["final"],
    ]
    updates = [event for event in events if isinstance(event, ItemUpdated)]
    assert [event.op for event in updates] == ["append", "append"]
    assert [event.source.native_cursor for event in updates] == ["11", "13"]
    assert not any(isinstance(event, ItemCompleted) for event in events)
    projection = _reduce(events).snapshot().items[0]
    assert projection.status == "open"
    assert [part.text for part in projection.parts] == ["final", "!"]
    assert len({part.part_id for part in projection.parts}) == 2


@pytest.mark.parametrize(
    ("append", "last_chunk", "want_event"),
    [
        (False, False, ItemSnapshotReplaced),
        (True, False, ItemUpdated),
        (False, True, ItemSnapshotReplaced),
        (True, True, ItemUpdated),
    ],
)
def test_trusted_last_chunk_only_controls_artifact_closure(
    append: bool,
    last_chunk: bool,
    want_event: type[RuntimeEvent],
) -> None:
    """Break caught: lastChunk changes append/replace semantics."""

    adapter = A2AEventAdapter()
    context = _context()
    if append:
        _map(adapter, _update("base", event_id="base"), context)
    events = _map(
        adapter,
        _update(
            "chunk",
            append=append,
            last_chunk=last_chunk,
            event_id="producer-occurrence-1",
        ),
        context,
    )

    assert any(isinstance(event, want_event) for event in events)
    assert not any(isinstance(event, ItemCompleted) for event in events)


def test_first_artifact_update_cannot_append_and_failure_is_transactional() -> None:
    """Break caught: append=True invents an empty base and consumes identity/seq state."""

    adapter = A2AEventAdapter()
    context = _context()

    with pytest.raises(A2AMappingError, match="append=True"):
        _map(
            adapter,
            _update("orphan", append=True, event_id="orphan-append"),
            context,
        )

    legal = _map(adapter, _update("base", event_id="base-replace"), context)
    assert [event.seq for event in legal] == [10, 11]
    assert isinstance(legal[0], ItemStarted)
    replacement = next(event for event in legal if isinstance(event, ItemSnapshotReplaced))
    assert [part.text for part in replacement.snapshot.parts] == ["base"]


def test_unidentified_last_chunk_remains_provisional_until_get_task() -> None:
    """Break caught: an at-least-once preview irreversibly closes before correction."""

    events = _map(
        A2AEventAdapter(),
        _update("preview", last_chunk=True),
        _context(),
    )

    assert not any(isinstance(event, ItemCompleted) for event in events)
    update = next(event for event in events if isinstance(event, ItemSnapshotReplaced))
    assert update.source.native_event_id is None
    assert update.source.metadata["provisional"] is True
    assert update.source.metadata["artifact_closed"] is True
    assert update.source.metadata["consistent"] is False


def test_duplicate_push_uses_extension_identity_or_stays_provisional() -> None:
    """Break caught: unidentified equal chunks are deduplicated by their text."""

    trusted_adapter = A2AEventAdapter()
    trusted_context = _context()
    _map(trusted_adapter, _update("base", event_id="producer-base"), trusted_context)
    trusted = _update("x", append=True, event_id="producer-event-1", cursor="44")
    first = _map(trusted_adapter, trusted, trusted_context)
    second = _map(trusted_adapter, trusted, trusted_context)
    assert first
    assert second == ()
    trusted_update = next(event for event in first if isinstance(event, ItemUpdated))
    assert trusted_update.source.native_event_id == "producer-event-1"
    assert trusted_update.source.native_cursor == "44"
    assert trusted_update.source.metadata["provisional"] is False

    provisional_adapter = A2AEventAdapter()
    provisional_context = _context()
    base = _map(provisional_adapter, _update("base"), provisional_context)
    native = _update("x", append=True)
    observed = (
        *base,
        *_map(provisional_adapter, native, provisional_context),
        *_map(provisional_adapter, native, provisional_context),
    )
    updates = [event for event in observed if isinstance(event, ItemUpdated)]
    assert len(updates) == 2
    assert updates[0].event_id != updates[1].event_id
    assert all(event.source.metadata["provisional"] is True for event in updates)
    assert [part.text for part in _reduce(observed).snapshot().items[0].parts] == [
        "base",
        "x",
        "x",
    ]


def test_message_parts_preserve_text_data_tool_and_file_types() -> None:
    """Break caught: every A2A Part is flattened into assistant text."""

    message = Message(
        message_id="message-7",
        context_id="context-9",
        task_id="task-9",
        role=Role.ROLE_AGENT,
        parts=[
            Part(text="answer"),
            Part(data=_value({"value": 7}), metadata={"part_id": "data-1"}),
            Part(
                data=_value({"city": "Beijing"}),
                metadata={
                    "kind": "tool_call",
                    "part_id": "call-part",
                    "call_id": "call-1",
                    "name": "weather",
                },
            ),
            Part(
                data=_value({"temperature": 20}),
                metadata={
                    "kind": "tool_result",
                    "part_id": "result-part",
                    "call_id": "call-1",
                },
            ),
            Part(
                url="https://example.invalid/report",
                filename="report.txt",
                media_type="text/plain",
            ),
        ],
        metadata={"event_id": "message-event-7", "seq": "50"},
    )

    events = _map(A2AEventAdapter(), message, _context())
    completed = next(event for event in events if isinstance(event, ItemCompleted))
    assert completed.source.native_item_id == "message-7"
    assert completed.source.native_event_id == "message-event-7"
    assert completed.source.native_cursor == "50"
    parts = completed.snapshot.parts
    assert [type(part) for part in parts] == [
        TextContent,
        DataContent,
        ToolCallContent,
        ToolResultContent,
        ArtifactContent,
    ]
    assert parts[2].call_id == parts[3].call_id == "call-1"
    assert parts[4].uri == "https://example.invalid/report"
    assert len({part.part_id for part in parts}) == 5


def test_input_required_emits_interaction_continuation_and_resume() -> None:
    """Break caught: A2A HITL is mislabeled as A2UI or generic text."""

    adapter = A2AEventAdapter()
    context = _context()
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
    first = _map(adapter, requested, context)
    resumed = _map(adapter, _working(), context, timestamp=1001.0)

    interaction = next(event for event in first if isinstance(event, InteractionRequested))
    continuation = next(event for event in first if isinstance(event, ContinuationCreated))
    interrupted = next(event for event in first if isinstance(event, RunInterrupted))
    assert interaction.interaction_kind == "structured_input"
    assert interaction.request.prompt == "Choose a region"
    assert interaction.request.schema_ == {"type": "string"}
    assert continuation.continuation_kind == "task_resume"
    assert continuation.ref == {"context_id": "context-9", "task_id": "task-9"}
    assert interrupted.interaction_id == interaction.interaction_id
    assert interrupted.continuation_id == continuation.continuation_id

    resolved = next(event for event in resumed if isinstance(event, InteractionResolved))
    continued = next(event for event in resumed if isinstance(event, ContinuationResumed))
    assert resolved.interaction_id == interaction.interaction_id
    assert continued.continuation_id == continuation.continuation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["terminal", "reconnect", "subscription_rebuild"])
async def test_reconciliation_paths_call_get_task_before_consistent_completion(
    reason: str,
) -> None:
    """Break caught: terminal/reconnect/rebuild trusts the delivered preview."""

    adapter = A2AEventAdapter()
    context = _context()
    live = (
        *_map(adapter, _working(), context),
        *_map(adapter, _update("wrong"), context),
        *_map(adapter, _terminal(), context),
    )
    assert not any(isinstance(event, RunCompleted) for event in live)
    snapshot = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                artifact_id="artifact-1",
                name="response",
                parts=[Part(text="authoritative")],
            )
        ],
        history=[
            Message(
                message_id="message-final",
                context_id="context-9",
                task_id="task-9",
                role=Role.ROLE_AGENT,
                parts=[Part(text="done")],
            )
        ],
    )
    client = _GetTaskClient(snapshot)

    result = await adapter.reconcile(client, context, reason=reason, timestamp=1002.0)

    assert [request.id for request in client.requests] == ["task-9"]
    assert result.consistent is True
    completed_items = [event for event in result.events if isinstance(event, ItemCompleted)]
    assert [event.snapshot.parts[0].text for event in completed_items] == [
        "authoritative",
        "done",
    ]
    terminal = next(event for event in result.events if isinstance(event, RunCompleted))
    assert terminal.source.metadata["consistent"] is True
    assert terminal.source.metadata["reconciliation_reason"] == reason
    assert [ref.item_id for ref in terminal.output_refs] == [
        event.item_id for event in completed_items
    ]
    projection = _reduce((*live, *result.events)).snapshot()
    assert [item.parts[0].text for item in projection.items] == ["authoritative", "done"]
    assert projection.status == "completed"


@pytest.mark.asyncio
async def test_get_task_failure_preserves_preview_without_consistency_claim() -> None:
    """Break caught: transport failure is presented as consistent remote success."""

    adapter = A2AEventAdapter()
    context = _context()
    live = (
        *_map(adapter, _working(), context),
        *_map(adapter, _update("preview"), context),
        *_map(adapter, _terminal(), context),
    )
    client = _GetTaskClient(RuntimeError("offline"))

    result = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        timestamp=1002.0,
    )

    assert result.consistent is False
    assert result.error == "get_task_failed"
    assert not any(isinstance(event, RunCompleted) for event in result.events)
    diagnostic = next(event for event in result.events if isinstance(event, RunProgress))
    assert diagnostic.source.metadata["consistent"] is False
    assert diagnostic.source.metadata["reconciliation_error"] == "RuntimeError"
    assert _reduce((*live, *result.events)).snapshot().items[0].parts[0].text == "preview"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "terminal_type"),
    [
        (TaskState.TASK_STATE_FAILED, RunFailed),
        (TaskState.TASK_STATE_REJECTED, RunFailed),
        (TaskState.TASK_STATE_CANCELED, RunCanceled),
    ],
)
async def test_authoritative_terminal_state_never_fabricates_success(
    state: int,
    terminal_type: type[RuntimeEvent],
) -> None:
    """Break caught: every terminal Task snapshot becomes run.completed."""

    adapter = A2AEventAdapter()
    context = _context()
    _map(adapter, _working(), context)
    _map(adapter, _terminal(state), context)
    client = _GetTaskClient(
        Task(
            id="task-9",
            context_id="context-9",
            status=TaskStatus(
                state=state,
                message=Message(
                    message_id="terminal-message",
                    task_id="task-9",
                    context_id="context-9",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text="remote terminal")],
                ),
            ),
        )
    )

    result = await adapter.reconcile(client, context, reason="terminal", timestamp=1002.0)

    assert result.consistent is True
    assert isinstance(result.events[-1], terminal_type)
    assert not any(isinstance(event, RunCompleted) for event in result.events)


def test_native_identity_is_deterministic_and_canonical_round_trips() -> None:
    """Break caught: ids use process UUID/time while source identity is stable."""

    native = _update(
        "stable",
        append=False,
        last_chunk=True,
        event_id="native-7",
        cursor="700",
    )
    left = _map(A2AEventAdapter(), native, _context())
    right = _map(A2AEventAdapter(), native, _context())

    assert [event.event_id for event in left] == [event.event_id for event in right]
    assert [getattr(event, "item_id", None) for event in left] == [
        getattr(event, "item_id", None) for event in right
    ]
    assert [event.seq for event in left] == [10, 11]
    assert all(
        dump_runtime_event(parse_runtime_event(dump_runtime_event(event)))
        == dump_runtime_event(event)
        for event in left
    )
    assert left[0].event_id != left[1].event_id


def test_unknown_truncated_ambiguous_and_identity_collision_fail_closed() -> None:
    """Break caught: malformed A2A payload silently becomes a text item."""

    adapter = A2AEventAdapter()
    context = _context()
    with pytest.raises(A2AMappingError, match="unsupported A2A event"):
        _map(adapter, object(), context)
    with pytest.raises(A2AMappingError, match="task_id"):
        _map(
            adapter,
            TaskArtifactUpdateEvent(
                context_id="context-9",
                artifact=Artifact(artifact_id="a", parts=[Part(text="x")]),
            ),
            context,
        )
    with pytest.raises(A2AMappingError, match="no supported payload"):
        _map(
            adapter,
            TaskArtifactUpdateEvent(
                task_id="task-9",
                context_id="context-9",
                artifact=Artifact(
                    artifact_id="a",
                    parts=[Part(filename="missing-payload.txt")],
                ),
                metadata={"event_id": "empty-part"},
            ),
            context,
        )

    _map(
        adapter,
        TaskArtifactUpdateEvent(
            task_id="task-9",
            context_id="context-9",
            artifact=Artifact(
                artifact_id="artifact-1",
                name="response",
                parts=[Part(text="text", metadata={"part_id": "shared-part"})],
            ),
            metadata={"event_id": "part-text"},
        ),
        context,
    )
    with pytest.raises(A2AMappingError, match="changed content type"):
        _map(
            adapter,
            TaskArtifactUpdateEvent(
                task_id="task-9",
                context_id="context-9",
                artifact=Artifact(
                    artifact_id="artifact-1",
                    name="response",
                    parts=[
                        Part(
                            data=_value({"not": "text"}),
                            metadata={"part_id": "shared-part"},
                        )
                    ],
                ),
                metadata={"event_id": "part-data"},
            ),
            context,
        )


@pytest.mark.asyncio
async def test_working_get_task_replaces_snapshot_atomically_without_closing_item() -> None:
    """Break caught: a reconnect snapshot closes a still-working remote artifact."""

    adapter = A2AEventAdapter()
    context = _context()
    live = (
        *_map(adapter, _working(), context),
        *_map(adapter, _update("stale", event_id="live-stale"), context),
    )
    client = _GetTaskClient(
        Task(
            id="task-9",
            context_id="context-9",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            artifacts=[
                Artifact(
                    artifact_id="artifact-1",
                    name="response",
                    parts=[Part(text="fresh")],
                )
            ],
        )
    )

    result = await adapter.reconcile(
        client,
        context,
        reason="reconnect",
        attempt_id="reconnect-1",
        timestamp=1002.0,
    )

    assert result.consistent is True
    assert result.terminal is False
    replacement = next(event for event in result.events if isinstance(event, ItemSnapshotReplaced))
    assert [part.text for part in replacement.snapshot.parts] == ["fresh"]
    assert not any(isinstance(event, ItemCompleted) for event in result.events)
    projection = _reduce((*live, *result.events)).snapshot()
    assert projection.status == "running"
    assert projection.items[0].status == "open"
    assert [part.text for part in projection.items[0].parts] == ["fresh"]


@pytest.mark.asyncio
async def test_reconcile_failure_rolls_back_shadow_state_and_placeholder_seq() -> None:
    """Break caught: partial snapshot mapping poisons retry state and consumes seq."""

    adapter = A2AEventAdapter()
    context = _context()
    _map(adapter, _working(), context)
    _map(adapter, _update("preview", event_id="preview-1"), context)
    bad = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                artifact_id="artifact-1",
                name="response",
                parts=[Part(text="corrected")],
            ),
            Artifact(
                artifact_id="artifact-2",
                name="response",
                parts=[Part(text="second")],
            ),
        ],
        history=[
            Message(
                message_id="bad-message",
                context_id="context-9",
                task_id="other-task",
                role=Role.ROLE_AGENT,
                parts=[Part(text="bad")],
            )
        ],
    )
    client = _GetTaskClient(bad)

    failed = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        attempt_id="terminal-attempt-1",
        timestamp=1002.0,
    )
    assert failed.consistent is False
    assert failed.error == "nested_message_identity_mismatch"
    assert failed.events[0].seq == 13

    client.result = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=list(bad.artifacts),
        history=[
            Message(
                message_id="good-message",
                role=Role.ROLE_AGENT,
                parts=[Part(text="good")],
            )
        ],
    )
    retried = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        attempt_id="terminal-attempt-2",
        timestamp=1003.0,
    )

    assert retried.consistent is True
    assert retried.terminal is True
    assert retried.events[0].seq == 13
    second_item_id = next(
        event.item_id
        for event in retried.events
        if isinstance(event, ItemStarted) and event.source.native_item_id == "artifact-2"
    )
    assert second_item_id


@pytest.mark.asyncio
async def test_nested_messages_inherit_outer_scope_but_nonempty_mismatch_fails() -> None:
    """Break caught: legal omitted nested ids fail or mismatched ids cross task scope."""

    inherited = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        history=[
            Message(
                message_id="nested-message",
                role=Role.ROLE_AGENT,
                parts=[Part(text="nested")],
            )
        ],
    )
    ok = await A2AEventAdapter().reconcile(
        _GetTaskClient(inherited),
        _context(),
        reason="terminal",
        attempt_id="nested-ok",
        timestamp=1000.0,
    )
    assert ok.consistent is True
    completed = next(event for event in ok.events if isinstance(event, ItemCompleted))
    assert completed.source.native_item_id == "nested-message"

    mismatched = Task()
    mismatched.CopyFrom(inherited)
    mismatched.history[0].context_id = "other-context"
    rejected = await A2AEventAdapter().reconcile(
        _GetTaskClient(mismatched),
        _context(),
        reason="terminal",
        attempt_id="nested-bad",
        timestamp=1000.0,
    )
    assert rejected.consistent is False
    assert rejected.error == "nested_message_identity_mismatch"


@pytest.mark.asyncio
async def test_task_status_is_required_and_terminal_reason_can_observe_working() -> None:
    """Break caught: missing/working status is reported as a terminal consistent result."""

    adapter = A2AEventAdapter()
    context = _context()
    with pytest.raises(A2AMappingError, match="Task.status"):
        _map(adapter, Task(id="task-9", context_id="context-9"), context)

    missing = await adapter.reconcile(
        _GetTaskClient(Task(id="task-9", context_id="context-9")),
        context,
        reason="terminal",
        attempt_id="missing-status",
        timestamp=1000.0,
    )
    assert missing.consistent is False
    assert missing.terminal is False
    assert missing.error == "missing_task_status"

    working = await adapter.reconcile(
        _GetTaskClient(
            Task(
                id="task-9",
                context_id="context-9",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        ),
        context,
        reason="terminal",
        attempt_id="terminal-race",
        timestamp=1001.0,
    )
    assert working.consistent is True
    assert working.terminal is False
    assert not any(isinstance(event, RunCompleted) for event in working.events)


def test_live_non_agent_message_cannot_become_final_output() -> None:
    """Break caught: a caller/user message is projected as the agent's answer."""

    adapter = A2AEventAdapter()
    context = _context()
    with pytest.raises(A2AMappingError, match="ROLE_AGENT"):
        _map(
            adapter,
            Message(
                message_id="user-message",
                context_id="context-9",
                task_id="task-9",
                role=Role.ROLE_USER,
                parts=[Part(text="input")],
            ),
            context,
        )

    legal = _map(adapter, _working(), context)
    assert legal[0].seq == 10


def test_duplicate_input_required_is_lifecycle_idempotent_and_collision_safe() -> None:
    """Break caught: duplicate push requests the same HITL interaction twice."""

    def requested(prompt: str, event_id: str) -> TaskStatusUpdateEvent:
        return TaskStatusUpdateEvent(
            task_id="task-9",
            context_id="context-9",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="input-stable",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text=prompt)],
                ),
            ),
            metadata={"event_id": event_id},
        )

    adapter = A2AEventAdapter()
    context = _context()
    first = _map(adapter, requested("Choose", "delivery-1"), context)
    duplicate = _map(adapter, requested("Choose", "delivery-2"), context)
    assert any(isinstance(event, InteractionRequested) for event in first)
    assert duplicate == ()

    with pytest.raises(A2AMappingError, match="interaction payload"):
        _map(adapter, requested("Changed", "delivery-3"), context)


def test_occurrence_id_uses_payload_fingerprint_and_cache_is_bounded() -> None:
    """Break caught: producer id collisions silently drop data or cache grows forever."""

    adapter = A2AEventAdapter()
    context = _context()
    original = _update("zero", event_id="event-0")
    first = _map(adapter, original, context)
    assert _map(adapter, original, context) == ()
    with pytest.raises(A2AMappingError, match="producer event_id"):
        _map(adapter, _update("collision", event_id="event-0"), context)
    with pytest.raises(A2AMappingError, match="producer event_id"):
        _map(
            adapter,
            Message(
                message_id="different-native-kind",
                context_id="context-9",
                task_id="task-9",
                role=Role.ROLE_AGENT,
                parts=[Part(text="message collision")],
                metadata={"event_id": "event-0"},
            ),
            context,
        )

    for index in range(1, adapter.OCCURRENCE_CACHE_LIMIT + 1):
        _map(adapter, _update(str(index), event_id=f"event-{index}"), context)

    replay_after_eviction = _map(adapter, original, context)
    assert first
    assert replay_after_eviction


@pytest.mark.asyncio
async def test_reconcile_diagnostic_attempt_is_stable_without_consuming_seq() -> None:
    """Break caught: one GetTask failure consumes two seq values and changes on retry."""

    adapter = A2AEventAdapter()
    context = _context()
    client = _GetTaskClient(RuntimeError("offline"))
    first = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        attempt_id="attempt-stable",
        timestamp=1000.0,
    )
    second = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        attempt_id="attempt-stable",
        timestamp=1000.0,
    )

    assert first.attempt_id == second.attempt_id == "attempt-stable"
    assert first.events[0].event_id == second.events[0].event_id
    assert first.events[0].seq == second.events[0].seq == 10
    legal = _map(adapter, _working(), context)
    assert legal[0].seq == 10


@pytest.mark.asyncio
async def test_repeated_terminal_snapshot_is_idempotent_and_collision_safe() -> None:
    """Break caught: a repeated GetTask terminal closes an already closed run again."""

    adapter = A2AEventAdapter()
    context = _context()
    task = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                artifact_id="artifact-1",
                name="response",
                parts=[Part(text="final")],
            )
        ],
    )
    client = _GetTaskClient(task)
    first = await adapter.reconcile(
        client,
        context,
        reason="terminal",
        attempt_id="terminal-1",
        timestamp=1000.0,
    )
    duplicate = await adapter.reconcile(
        client,
        context,
        reason="reconnect",
        attempt_id="terminal-2",
        timestamp=1001.0,
    )

    assert any(isinstance(event, RunCompleted) for event in first.events)
    assert duplicate.consistent is True
    assert duplicate.terminal is True
    assert duplicate.events == ()

    changed = Task()
    changed.CopyFrom(task)
    changed.artifacts[0].parts[0].text = "changed"
    client.result = changed
    collision = await adapter.reconcile(
        client,
        context,
        reason="reconnect",
        attempt_id="terminal-3",
        timestamp=1002.0,
    )
    assert collision.consistent is False
    assert collision.error == "terminal_snapshot_collision"


def test_message_only_response_without_task_id_is_a_complete_run() -> None:
    """Break caught: legal direct Message fails identity or leaves its run open."""

    context = A2AAdapterContext(
        run_id="direct-runtime-run",
        context_id="direct-context",
        task_id=None,
        initial_seq=20,
    )
    native = Message(
        message_id="direct-message",
        context_id="direct-context",
        role=Role.ROLE_AGENT,
        parts=[Part(text="direct answer")],
    )

    events = _map(A2AEventAdapter(), native, context)

    assert isinstance(events[0], RunStarted)
    assert isinstance(events[-1], RunCompleted)
    completed = next(event for event in events if isinstance(event, ItemCompleted))
    assert events[-1].output_refs[0].item_id == completed.item_id
    assert completed.source.native_run_id == "direct-message"
    assert completed.scope_id == events[0].scope_id == events[-1].scope_id
    projection = _reduce(events).snapshot()
    assert projection.status == "completed"
    assert projection.items[0].parts[0].text == "direct answer"


def test_message_only_empty_task_id_inherits_bound_task_context() -> None:
    """Break caught: an optional Message.task_id is treated as required."""

    events = _map(
        A2AEventAdapter(),
        Message(
            message_id="direct-task-message",
            context_id="context-9",
            role=Role.ROLE_AGENT,
            parts=[Part(text="direct task answer")],
        ),
        _context(),
    )

    assert isinstance(events[0], RunStarted)
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].source.native_run_id == "task-9"
    assert _reduce(events).snapshot().status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED],
)
async def test_subscription_rebuild_restores_native_hitl_lifecycle(state: int) -> None:
    """Break caught: GetTask HITL becomes a final message plus running progress."""

    task = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(
            state=state,
            message=Message(
                message_id="restore-input",
                role=Role.ROLE_AGENT,
                parts=[Part(text="Provide input")],
                metadata={"input_schema": {"type": "string"}},
            ),
        ),
    )

    result = await A2AEventAdapter().reconcile(
        _GetTaskClient(task),
        _context(),
        reason="subscription_rebuild",
        attempt_id=f"restore-{state}",
        timestamp=1000.0,
    )

    assert result.consistent is True
    assert result.terminal is False
    assert any(isinstance(event, InteractionRequested) for event in result.events)
    assert any(isinstance(event, ContinuationCreated) for event in result.events)
    assert any(isinstance(event, RunInterrupted) for event in result.events)
    assert not any(isinstance(event, RunProgress) for event in result.events)
    assert not any(
        isinstance(event, ItemCompleted) and event.source.native_item_id == "restore-input"
        for event in result.events
    )


def test_duplicate_native_part_id_fails_transactionally_before_reducer() -> None:
    """Break caught: duplicate source part ids escape as an invalid snapshot."""

    adapter = A2AEventAdapter()
    context = _context()
    duplicate = TaskArtifactUpdateEvent(
        task_id="task-9",
        context_id="context-9",
        artifact=Artifact(
            artifact_id="artifact-duplicate",
            name="response",
            parts=[
                Part(text="first", metadata={"part_id": "duplicate"}),
                Part(text="second", metadata={"part_id": "duplicate"}),
            ],
        ),
        metadata={"event_id": "duplicate-parts"},
    )

    with pytest.raises(A2AMappingError, match="duplicate part_id"):
        _map(adapter, duplicate, context)

    legal = _map(
        adapter,
        _update("legal", artifact_id="artifact-duplicate", event_id="legal-parts"),
        context,
    )
    assert [event.seq for event in legal] == [10, 11]


@pytest.mark.asyncio
async def test_working_snapshot_removal_requires_new_base_before_append() -> None:
    """Break caught: append resurrects an artifact removed by authoritative state."""

    adapter = A2AEventAdapter()
    context = _context()
    _map(adapter, _update("base", event_id="base"), context)
    removed = await adapter.reconcile(
        _GetTaskClient(
            Task(
                id="task-9",
                context_id="context-9",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        ),
        context,
        reason="reconnect",
        attempt_id="removed",
        timestamp=1001.0,
    )
    replacement = next(event for event in removed.events if isinstance(event, ItemSnapshotReplaced))
    assert replacement.snapshot.parts == ()

    with pytest.raises(A2AMappingError, match="artifact_missing"):
        _map(
            adapter,
            _update("invalid", append=True, event_id="append-after-remove"),
            context,
        )

    recreated = _map(adapter, _update("new-base", event_id="new-base"), context)
    assert any(isinstance(event, ItemSnapshotReplaced) for event in recreated)


@pytest.mark.asyncio
async def test_terminal_history_duplicate_message_id_has_one_output_ref() -> None:
    """Break caught: repeated history delivery duplicates terminal output_refs."""

    message = Message(
        message_id="history-message",
        role=Role.ROLE_AGENT,
        parts=[Part(text="once")],
    )
    task = Task(
        id="task-9",
        context_id="context-9",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        history=[message, message],
    )

    result = await A2AEventAdapter().reconcile(
        _GetTaskClient(task),
        _context(),
        reason="terminal",
        attempt_id="duplicate-history",
        timestamp=1000.0,
    )

    terminal = next(event for event in result.events if isinstance(event, RunCompleted))
    assert len(terminal.output_refs) == 1
    assert len({(ref.scope_id, ref.item_id) for ref in terminal.output_refs}) == 1


def test_consecutive_native_hitl_supersedes_without_reinterrupting_run() -> None:
    """Break caught: a second native prompt overwrites state and interrupts twice."""

    adapter = A2AEventAdapter()
    context = _context()

    def request(message_id: str, prompt: str) -> TaskStatusUpdateEvent:
        return TaskStatusUpdateEvent(
            task_id="task-9",
            context_id="context-9",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id=message_id,
                    role=Role.ROLE_AGENT,
                    parts=[Part(text=prompt)],
                ),
            ),
            metadata={"event_id": f"event-{message_id}"},
        )

    first = _map(adapter, request("input-one", "First input"), context)
    second = _map(adapter, request("input-two", "Second input"), context)
    combined = (*first, *second)

    assert sum(isinstance(event, RunInterrupted) for event in combined) == 1
    assert isinstance(second[0], InteractionResolved)
    projection = _reduce(combined).snapshot()
    assert [interaction.status for interaction in projection.interactions] == [
        "resolved",
        "requested",
    ]


@pytest.mark.asyncio
async def test_terminal_snapshot_resolves_active_native_interaction() -> None:
    """Break caught: a terminal GetTask leaves a stale requested interaction."""

    adapter = A2AEventAdapter()
    context = _context()
    requested = _map(
        adapter,
        TaskStatusUpdateEvent(
            task_id="task-9",
            context_id="context-9",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="input-before-terminal",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text="Input")],
                ),
            ),
            metadata={"event_id": "input-before-terminal"},
        ),
        context,
    )
    terminal = await adapter.reconcile(
        _GetTaskClient(
            Task(
                id="task-9",
                context_id="context-9",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        ),
        context,
        reason="terminal",
        attempt_id="terminal-after-input",
        timestamp=1001.0,
    )

    assert terminal.consistent is True
    assert any(isinstance(event, InteractionResolved) for event in terminal.events)
    projection = _reduce((*requested, *terminal.events)).snapshot()
    assert projection.status == "completed"
    assert all(interaction.status == "resolved" for interaction in projection.interactions)


def test_explicit_empty_oneof_parts_are_preserved() -> None:
    """Break caught: protobuf oneof presence is inferred from value truthiness."""

    events = _map(
        A2AEventAdapter(),
        Message(
            message_id="empty-oneofs",
            context_id="context-9",
            role=Role.ROLE_AGENT,
            parts=[
                Part(text=""),
                Part(raw=b"", filename="empty.bin"),
                Part(url="", filename="empty.url"),
            ],
        ),
        _context(),
    )

    completed = next(event for event in events if isinstance(event, ItemCompleted))
    assert [type(part) for part in completed.snapshot.parts] == [
        TextContent,
        ArtifactContent,
        ArtifactContent,
    ]
    assert completed.snapshot.parts[0].text == ""
    assert completed.snapshot.parts[1].data == {"base64": ""}
    assert completed.snapshot.parts[2].uri == ""
