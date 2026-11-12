"""Use the installed ADK converter, never a plain LLM snapshot substitute."""

from importlib.metadata import version
from types import SimpleNamespace

import pytest
from a2a.types import Artifact, Part, Task, TaskArtifactUpdateEvent, TaskState, TaskStatus
from packaging.version import Version

if Version(version("google-adk")) < Version("2.5.0"):
    pytest.skip(
        "Upstream RemoteA2aAgent wire 1.0 converters require google-adk >=2.5.0",
        allow_module_level=True,
    )

from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

from ksadk.events.canonical import ItemCompleted, ItemStarted, ItemUpdated
from ksadk.events.identity import stable_item_id, stable_scope_id
from ksadk.events.reducer import StreamReducer
from ksadk.runners._adk_a2a_stream import preserve_a2a_stream_identity
from tests.runners.test_adk_multi_llm_runtime_events import runner_for


@pytest.mark.asyncio
async def test_same_native_id_remote_frames_keep_equal_chunks_and_deduplicate_replay():
    from google.adk.events import Event

    agent = RemoteA2aAgent(name="remote", agent_card="https://example.com/card")
    preserve_a2a_stream_identity(agent)
    ctx = SimpleNamespace(invocation_id="native-run", branch="remote")
    native = []
    for index in range(3):
        event = agent._config.a2a_artifact_update_converter(
            TaskArtifactUpdateEvent(
                task_id="t",
                artifact=Artifact(artifact_id="a", parts=[Part(text="same")]),
                append=index > 0,
                last_chunk=index == 2,
            ),
            "remote",
            ctx,
        )
        # New ADK converters reuse the artifact response ID across deliveries.
        # Exercise that behavior under both supported upstream converters.
        event.id = "shared-response"
        native.extend((event, Event.model_validate(event.model_dump(mode="json"))))

    events = [
        event async for event in runner_for(native).stream_canonical_events(
            {"input": "test", "run_id": "run"}
        )
    ]
    started = [event for event in events if isinstance(event, ItemStarted)]
    completed = [event for event in events if isinstance(event, ItemCompleted)]
    updates = [event for event in events if isinstance(event, ItemUpdated)]
    assert len(started) == len(completed) == 1
    assert len(updates) == 3
    assert len({event.source.native_event_id for event in updates}) == 3
    assert all(
        event.source.metadata["adk_response_event_id"] == "shared-response" for event in updates
    )
    mutations = [*started, *updates, *completed]
    assert len({event.event_id for event in mutations}) == len(mutations)
    assert completed[0].snapshot.parts[0].text == "samesamesame"
    assert completed[0].item_id == started[0].item_id
    assert completed[0].scope_id == started[0].scope_id
    assert completed[0].scope_id == stable_scope_id("adk", "native-run", "remote")
    assert completed[0].item_id == stable_item_id(
        "adk", "native-run", "remote", stable_item_id("a2a", "t", "a"), "message"
    )
    replayed = [
        event async for event in runner_for(
            [Event.model_validate(event.model_dump(mode="json")) for event in native]
        ).stream_canonical_events({"input": "test", "run_id": "run"})
        if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
    ]
    assert [event.model_dump() for event in replayed] == [
        event.model_dump() for event in events
        if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
    ]


def remote_events(*, terminal=True, replacement=None, artifact_id="a", task_id="t"):
    agent = RemoteA2aAgent(name="remote", agent_card="https://example.com/card")
    preserve_a2a_stream_identity(agent)
    ctx = SimpleNamespace(invocation_id="native-run", branch="remote")
    config = agent._config
    events = []
    for text, append, last in [("hello ", False, False), ("world", True, True)]:
        event = config.a2a_artifact_update_converter(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                artifact=Artifact(artifact_id=artifact_id, parts=[Part(text=text)]),
                append=append,
                last_chunk=last,
            ),
            "remote",
            ctx,
        )
        events.append(event)
    assert events[-1].partial is False  # Real ADK last-chunk behavior.
    if terminal:
        events.append(
            config.a2a_task_converter(
                Task(
                    id=task_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    artifacts=[
                        Artifact(
                            artifact_id=artifact_id, parts=[Part(text=replacement or "hello world")]
                        )
                    ],
                ),
                "remote",
                ctx,
            )
        )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,replacement", [(False, None), (True, None), (True, "corrected")])
async def test_real_converter_last_increment_and_terminal_reconcile(terminal, replacement):
    native = remote_events(terminal=terminal, replacement=replacement)
    events = [
        e
        async for e in runner_for(native).stream_canonical_events(
            {"input": "test", "run_id": "run"}
        )
    ]
    reducer = StreamReducer()
    for event in events:
        reducer.apply(event)
    assert len([e for e in events if isinstance(e, ItemStarted)]) == 1
    completed = [e for e in events if isinstance(e, ItemCompleted)]
    assert len(completed) == 1
    assert completed[0].snapshot.parts[0].text == (replacement or "hello world")
    chunks = [e async for e in runner_for(native).stream({"input": "test", "run_id": "run"})]
    assert chunks[-1] == {"type": "final", "output": replacement or "hello world"}


@pytest.mark.asyncio
async def test_equal_text_distinct_artifacts_and_tasks_survive():
    first = remote_events(artifact_id="a")
    second = remote_events(artifact_id="b")
    third = remote_events(artifact_id="a", task_id="other-task")
    native = [first[0], second[0], first[1], second[1], first[2], second[2], *third]
    chunks = [e async for e in runner_for(native).stream({"input": "test", "run_id": "run"})]
    assert chunks[-1] == {"type": "final", "output": "hello world" * 3}


@pytest.mark.asyncio
async def test_terminal_task_reconciles_each_artifact_and_replay_is_identical():
    agent = RemoteA2aAgent(name="remote", agent_card="https://example.com/card")
    preserve_a2a_stream_identity(agent)
    ctx = SimpleNamespace(invocation_id="native-run", branch="remote")
    native = remote_events(terminal=False, artifact_id="a") + remote_events(
        terminal=False, artifact_id="b"
    )
    task = Task(
        id="t",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(artifact_id="a", parts=[Part(text="corrected a")]),
            Artifact(artifact_id="b", parts=[Part(text="corrected b")]),
        ],
    )
    native.append(agent._config.a2a_task_converter(task, "remote", ctx))
    events = [
        e
        async for e in runner_for(native).stream_canonical_events(
            {"input": "test", "run_id": "run"}
        )
    ]
    snapshots = []
    for split in (0, len(events) // 2, len(events)):
        reducer = StreamReducer()
        for event in events[:split]:
            reducer.apply(event)
        for event in events[split:]:
            reducer.apply(event)
        snapshots.append(reducer.snapshot())
    assert snapshots[0] == snapshots[1] == snapshots[2]
    completed = [e.snapshot.parts[0].text for e in events if isinstance(e, ItemCompleted)]
    assert completed == ["corrected a", "corrected b"]


@pytest.mark.asyncio
async def test_working_snapshot_remains_open_until_terminal():
    native = remote_events()
    provisional = native[-1].model_copy(deep=True)
    provisional.id += "-working"
    for observation in provisional.custom_metadata["ksadk_a2a_stream"]:
        observation["snapshot"] = False
        observation["last_chunk"] = False
    events = [
        e
        async for e in runner_for([*native[:2], provisional, native[-1]]).stream_canonical_events(
            {"input": "test", "run_id": "run"}
        )
    ]
    assert len([e for e in events if isinstance(e, ItemCompleted)]) == 1


def test_nested_installation_is_idempotent_and_preserves_configured_converter():
    agent = RemoteA2aAgent(name="remote", agent_card="https://example.com/card")
    original = agent._config.a2a_artifact_update_converter
    root = SimpleNamespace(sub_agents=[agent])
    preserve_a2a_stream_identity(root)
    wrapped = agent._config.a2a_artifact_update_converter
    preserve_a2a_stream_identity(root)
    assert wrapped is agent._config.a2a_artifact_update_converter
    assert wrapped.__wrapped__ is original


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_closed_snapshot_is_noop_or_explicit_failure(conflict):
    from ksadk.events.canonical import RunCompleted, RunFailed

    native = remote_events()
    duplicate = native[-1].model_copy(deep=True)
    duplicate.id += "-late"
    if conflict:
        duplicate.custom_metadata["ksadk_a2a_stream"][0]["text"]["message"] = "conflicting"
    events = [
        e
        async for e in runner_for([*native, duplicate]).stream_canonical_events(
            {"input": "test", "run_id": "run"}
        )
    ]
    assert isinstance(events[-1], RunFailed if conflict else RunCompleted)
    if conflict:
        assert events[-1].error.code == "A2A_TERMINAL_CONTENT_CONFLICT"
    assert len([e for e in events if isinstance(e, ItemCompleted)]) == 1


@pytest.mark.asyncio
async def test_append_false_replaces_only_its_artifact():
    native = remote_events(terminal=False)
    replacement = native[-1].model_copy(deep=True)
    replacement.id += "-replace"
    observation = replacement.custom_metadata["ksadk_a2a_stream"][0]
    observation["append"] = False
    observation["text"]["message"] = "replacement"
    chunks = [
        e
        async for e in runner_for([*native, replacement]).stream({"input": "test", "run_id": "run"})
    ]
    assert chunks[-1] == {"type": "final", "output": "replacement"}


@pytest.mark.asyncio
async def test_real_completed_status_summary_does_not_become_another_item():
    from a2a.types import Message, Role, TaskStatusUpdateEvent

    agent = RemoteA2aAgent(name="remote", agent_card="https://example.com/card")
    preserve_a2a_stream_identity(agent)
    ctx = SimpleNamespace(invocation_id="native-run", branch="remote")
    native = remote_events(terminal=False)
    status = TaskStatus(
        state=TaskState.TASK_STATE_COMPLETED,
        message=Message(
            message_id="summary", role=Role.ROLE_AGENT, parts=[Part(text="hello world")]
        ),
    )
    task = Task(
        id="t",
        status=status,
        artifacts=[Artifact(artifact_id="a", parts=[Part(text="hello world")])],
    )
    update = TaskStatusUpdateEvent(task_id="t", status=status)
    event = agent._config.a2a_status_update_converter(update, "remote", ctx)
    for hook in agent._config.request_interceptors:
        if hook.after_request:
            event = await hook.after_request(ctx, (task, update), event)
    native.append(event)
    chunks = [e async for e in runner_for(native).stream({"input": "test", "run_id": "run"})]
    assert chunks == [
        {"type": "text", "delta": "hello ", "replace": True},
        {"type": "text", "delta": "world"},
        {"type": "final", "output": "hello world"},
    ]
