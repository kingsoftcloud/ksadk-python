"""Regression coverage for identity-aware ADK multi-response streaming."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.genai import types

from ksadk.events.canonical import (
    ItemCompleted,
    ItemFailed,
    RunCanceled,
    RunCompleted,
    RunFailed,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.runners.adk_runner import ADKRunner


def two_llm_fixture() -> dict[str, object]:
    path = Path(__file__).parents[1] / "fixtures" / "adk_two_llm_outputs.json"
    return json.loads(path.read_text(encoding="utf-8"))


def native_text_event(text: str, event_id: str, *, partial: bool) -> Event:
    # ADK 2.x identifies workflow nodes with slash-separated node_info.path;
    # ADK 1.x exposes the agent hierarchy through dotted branch names.
    identity = (
        {"node_info": {"path": "root/worker"}}
        if "node_info" in Event.model_fields
        else {"branch": "root.worker"}
    )
    return Event(
        id=event_id,
        invocation_id="native-run",
        author="worker",
        partial=partial,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        **identity,
    )


def runner_for(events: list[Event]) -> ADKRunner:
    runner = ADKRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"),
        ".",
    )
    runner._agent = SimpleNamespace(name="root")  # noqa: SLF001
    runner._prepare_trace_metadata = lambda _: (None, [], None, "root")  # noqa: SLF001
    runner._extract_event_usage = lambda _: {}  # noqa: SLF001

    async def ensure_session(_: str | None) -> str:
        return "session-1"

    async def prepare_events(**_: object):
        async def stream():
            for event in events:
                yield event

        return stream()

    runner._ensure_session = ensure_session  # type: ignore[method-assign]  # noqa: SLF001
    runner._prepare_run_events = prepare_events  # type: ignore[method-assign]  # noqa: SLF001
    return runner


@pytest.mark.asyncio
async def test_kernel_owned_runner_and_managed_child_share_public_run_id():
    from ksadk.a2a.adk import _project
    from ksadk.a2a.runtime_approval import approval_event
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.sessions.in_memory import InMemorySessionService
    from tests.a2a.test_invocation_service_r85 import request

    req = request()
    canonical = approval_event(req, timestamp=1.0)
    native = _project(canonical, "expert", SimpleNamespace(invocation_id="native-1", branch=None))
    authority = RunEventAuthority(
        SessionServiceEventStore(InMemorySessionService()), session_id="s",
        run_id="public-run", guard=ActivationWriteGuard(activation_id="test", fencing_token=1),
    )
    with bind_run_event_authority(authority):
        result = [event async for event in runner_for([native]).stream_canonical_events({
            "run_id": "private-adapter-run", "session_id": "s"
        })]
        assert result
        assert all(event.run_id == "public-run" for event in result)
        assert canonical in result
        assert not any(event.event_type == "run.failed" for event in result)
        with pytest.raises(ValueError, match="identity mismatch"):
            _ = [event async for event in runner_for([]).stream_canonical_events({
                "run_id": "private-adapter-run", "session_id": "other-session"
            })]


def two_llm_responses() -> list[Event]:
    events: list[Event] = []
    responses = two_llm_fixture()["responses"]
    assert isinstance(responses, list)
    for response in responses:
        assert isinstance(response, dict)
        item_id = str(response["item_id"])
        partials = response["partials"]
        assert isinstance(partials, list)
        for index, partial in enumerate(partials, start=1):
            events.append(
                native_text_event(
                    str(partial), f"{item_id}-partial-{index}", partial=True
                )
            )
        events.append(
            native_text_event(
                str(response["terminal"]), f"{item_id}-terminal", partial=False
            )
        )
    return events


@pytest.mark.asyncio
async def test_two_llm_responses_do_not_append_the_second_terminal_snapshot() -> None:
    chunks = [
        chunk
        async for chunk in runner_for(two_llm_responses()).stream(
            {"input": "test", "run_id": "run-1"}
        )
    ]

    rendered = ""
    for chunk in chunks:
        if chunk.get("type") != "text":
            continue
        rendered = chunk["delta"] if chunk.get("replace") else rendered + chunk["delta"]

    assert rendered == two_llm_fixture()["expected_text"]
    assert chunks[-1] == {"type": "final", "output": rendered}


@pytest.mark.asyncio
async def test_explicit_output_snapshot_replaces_prior_legacy_output() -> None:
    old = native_text_event("old", "llm-1-terminal", partial=False)
    replacement = native_text_event("new", "llm-2-terminal", partial=False)
    replacement.content.parts[0].part_metadata = {"ksadk_output_snapshot": True}

    chunks = [
        chunk
        async for chunk in runner_for([old, replacement]).stream(
            {"input": "test", "run_id": "run-1"}
        )
    ]

    assert chunks == [
        {"type": "text", "delta": "old"},
        {"type": "text", "delta": "new", "replace": True},
        {"type": "final", "output": "new"},
    ]


@pytest.mark.asyncio
async def test_canonical_stream_preserves_distinct_equal_response_identities() -> None:
    runner = runner_for(
        [
            native_text_event("same", "llm-1", partial=False),
            native_text_event("same", "llm-2", partial=False),
        ]
    )

    events = [
        event
        async for event in runner.stream_canonical_events(
            {"input": "test", "run_id": "run-1"}
        )
    ]

    completed_messages = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "message"
    ]
    assert [
        part.text
        for event in completed_messages
        for part in event.snapshot.parts
        if isinstance(part, TextContent)
    ] == ["same", "same"]
    assert len({event.item_id for event in completed_messages}) == 2
    expected_path = "root/worker" if "node_info" in Event.model_fields else "root.worker"
    assert {event.source.metadata["path_key"] for event in completed_messages} == {expected_path}
    terminal = next(event for event in events if isinstance(event, RunCompleted))
    assert len(terminal.output_refs) == 2


@pytest.mark.asyncio
async def test_incomplete_native_stream_fails_open_items_and_the_run() -> None:
    events = [
        event
        async for event in runner_for(
            [native_text_event("unfinished", "llm-1-partial", partial=True)]
        ).stream_canonical_events({"input": "test", "run_id": "run-1"})
    ]

    failed_items = [event for event in events if isinstance(event, ItemFailed)]
    assert len(failed_items) == 1
    assert failed_items[0].error.code == "incomplete_stream"
    assert isinstance(events[-1], RunFailed)
    assert events[-1].error.code == "incomplete_stream"


@pytest.mark.asyncio
async def test_stream_end_closes_partial_reasoning_but_fails_partial_message() -> None:
    thought = native_text_event("thinking", "thought-partial", partial=True)
    thought.content.parts[0].thought = True
    events = [
        event
        async for event in runner_for(
            [thought, native_text_event("unfinished", "text-partial", partial=True)]
        ).stream_canonical_events({"run_id": "run-1"})
    ]

    assert any(
        isinstance(event, ItemCompleted) and event.item_kind == "reasoning"
        for event in events
    )
    assert any(
        isinstance(event, ItemFailed) and event.item_kind == "message" for event in events
    )
    assert isinstance(events[-1], RunFailed)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["confirm", [1, 2], 7, {"path": "out.txt"}, None])
async def test_native_tool_confirmation_preserves_json_payload(payload: object) -> None:
    from google.adk.events import EventActions
    from google.adk.tools.tool_confirmation import ToolConfirmation

    from ksadk.events.canonical import InteractionRequested, RunInterrupted

    event = Event(
        id="approval",
        invocation_id="native-run",
        author="root",
        actions=EventActions(
            requested_tool_confirmations={"call-write": ToolConfirmation(payload=payload)}
        ),
    )
    events = [
        event
        async for event in runner_for([event]).stream_canonical_events(
            {"run_id": "run-1"}
        )
    ]

    approval = next(event for event in events if isinstance(event, InteractionRequested))
    assert approval.request.call_id == "call-write"
    assert approval.request.detail["payload"] == payload
    assert isinstance(events[-1], RunInterrupted)


@pytest.mark.asyncio
async def test_legacy_usage_retains_last_model_window_separately_from_total() -> None:
    runner = runner_for(
        [
            native_text_event("first", "llm-1-terminal", partial=False),
            native_text_event("second", "llm-2-terminal", partial=False),
        ]
    )
    runner._extract_event_usage = lambda event: {  # type: ignore[method-assign]  # noqa: SLF001
        "input_tokens": 10 if event.id == "llm-1-terminal" else 20,
        "output_tokens": 2,
        "total_tokens": 12 if event.id == "llm-1-terminal" else 22,
    }

    chunks = [chunk async for chunk in runner.stream({"run_id": "run-1"})]

    assert chunks[-1]["usage"]["input_tokens"] == 30
    assert chunks[-1]["metadata"]["last_usage"]["input_tokens"] == 20


@pytest.mark.asyncio
async def test_gateway_approval_result_interrupts_the_canonical_run() -> None:
    from ksadk.events.canonical import InteractionRequested, RunInterrupted

    event = Event(
        id="tool-result",
        invocation_id="native-run",
        author="root",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-write",
                        name="write_file",
                        response={
                            "type": "approval_required",
                            "approval_request": {
                                "id": "appr-write",
                                "tool_name": "write_file",
                                "args": {"path": "out.txt"},
                            },
                        },
                    )
                )
            ],
        ),
    )

    events = [
        item
        async for item in runner_for([event]).stream_canonical_events(
            {"run_id": "run-1"}
        )
    ]

    approval = next(item for item in events if isinstance(item, InteractionRequested))
    assert approval.request.call_id == "call-write"
    assert approval.request.detail["approval_request_id"] == "appr-write"
    assert isinstance(events[-1], RunInterrupted)


@pytest.mark.asyncio
async def test_legacy_projection_spells_cancelled_for_existing_consumers() -> None:
    from ksadk.runners._adk_canonical_stream import legacy_chunks

    async def canceled_events():
        yield RunCanceled(
            schema_version=2,
            event_id="event-canceled",
            seq=1,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(framework="adk"),
            status="canceled",
            reason="user_requested",
        )

    chunks = [chunk async for chunk in legacy_chunks(canceled_events())]

    assert chunks == [{"type": "cancelled", "reason": "user_requested"}]


@pytest.mark.asyncio
async def test_native_error_without_text_never_completes_successfully() -> None:
    event = Event(
        id="failure",
        invocation_id="native-run",
        author="root",
        error_code="fixture_error",
    )

    events = [
        item
        async for item in runner_for([event]).stream_canonical_events(
            {"run_id": "run-1"}
        )
    ]

    assert isinstance(events[-1], RunFailed)
    assert events[-1].error.code == "adk_stream_error"


@pytest.mark.asyncio
async def test_closing_legacy_stream_closes_the_native_iterator() -> None:
    closed: list[bool] = []
    runner = runner_for([])

    async def prepare_events(**_: object):
        async def stream():
            try:
                yield native_text_event("partial", "partial-1", partial=True)
                yield native_text_event("partial", "terminal-1", partial=False)
            finally:
                closed.append(True)

        return stream()

    runner._prepare_run_events = prepare_events  # type: ignore[method-assign]  # noqa: SLF001
    stream = runner.stream({"run_id": "run-1"})
    assert (await anext(stream))["delta"] == "partial"

    await stream.aclose()

    assert closed == [True]


@pytest.mark.asyncio
async def test_runtime_store_live_full_and_cursor_replay_keep_two_responses() -> None:
    from ksadk.events.canonical_replay import replay_projection
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.events.reducer import StreamReducer
    from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry
    from ksadk.runtime.conversation_execution import iter_runtime_conversation_events
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
    from ksadk.sessions.in_memory import InMemorySessionService

    runner = runner_for(two_llm_responses())
    runner.detection = SimpleNamespace(type=SimpleNamespace(value="adk"))
    registry = RuntimeRegistry()
    registry.register("adk", lambda _: RunnerRuntimeAdapter(runner, runtime_type="adk"))
    service = InMemorySessionService()

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(runtime_type="adk", project_dir="."),
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "hi"}],
            session_id="session-1",
            model="fixture",
            session_service_provider=lambda: service,
        )
    ]

    live_reducer = StreamReducer()
    for event in events:
        live_reducer.apply(event)
        duplicate = live_reducer.apply(event)
        assert duplicate.applied is False
    live = live_reducer.snapshot()
    store = RuntimeEventStore(service)
    full_replay = await replay_projection(
        store,
        "session-1",
        run_id=events[0].run_id,
    )

    breakpoint = events[len(events) // 2].seq
    cursor_reducer = StreamReducer()
    for event in await store.list(
        "session-1",
        run_id=events[0].run_id,
        before_seq=breakpoint + 1,
    ):
        cursor_reducer.apply(event)
    for event in await store.list(
        "session-1",
        run_id=events[0].run_id,
        after_seq=breakpoint,
    ):
        cursor_reducer.apply(event)

    assert live == full_replay == cursor_reducer.snapshot()
    assert live.status == "completed"
    assert [
        part.text
        for item in live.items
        for part in item.parts
        if isinstance(part, TextContent)
    ] == [response["terminal"] for response in two_llm_fixture()["responses"]]
    assert len(live.output_refs) == 2


@pytest.mark.asyncio
async def test_real_adk_two_llm_calls_reach_responses_sse_once_each() -> None:
    from google.adk.agents import LlmAgent, SequentialAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService as ADKSessionService

    from ksadk.conversations.runtime_streaming import stream_responses_conversation_turn
    from ksadk.sessions.in_memory import InMemorySessionService
    from tests.a2a.test_reasoning_streaming import _sse_payloads

    class FixtureLlm(BaseLlm):
        model: str = "fixture"

        async def generate_content_async(self, llm_request, stream=False):
            del llm_request, stream
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="same ")],
                ),
                partial=True,
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="answer")],
                ),
                partial=True,
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="same answer")],
                ),
                partial=False,
            )

    root = SequentialAgent(
        name="root",
        sub_agents=[
            LlmAgent(name="worker_one", model=FixtureLlm()),
            LlmAgent(name="worker_two", model=FixtureLlm()),
        ],
    )
    adk_sessions = ADKSessionService()
    await adk_sessions.create_session(
        app_name="fixture",
        user_id="user",
        session_id="native-session",
    )
    adk_runner = Runner(
        agent=root,
        app_name="fixture",
        session_service=adk_sessions,
    )
    runner = runner_for([])
    runner._agent = root  # noqa: SLF001
    runner.detection = SimpleNamespace(type=SimpleNamespace(value="adk"), name="fixture")

    async def prepare_events(**kwargs: object):
        return adk_runner.run_async(
            user_id="user",
            session_id="native-session",
            new_message=types.Content(role="user", parts=[types.Part(text="hi")]),
            run_config=kwargs["run_config"],
        )

    runner._prepare_run_events = prepare_events  # type: ignore[method-assign]  # noqa: SLF001
    service = InMemorySessionService()

    frames = [
        frame
        async for frame in stream_responses_conversation_turn(
            runner=runner,
            agent_id="fixture",
            user_id="user",
            session_id="session-1",
            messages=[{"role": "user", "content": "hi"}],
            model=None,
            prepare_runner=lambda *_: None,
            session_service_provider=lambda: service,
        )
    ]

    completed = _sse_payloads(frames, "response.completed")
    assert completed, _sse_payloads(frames, "response.failed")
    assert completed[-1]["output_text"] == "same answersame answer"
    deltas = _sse_payloads(frames, "response.output_text.delta")
    assert [payload["delta"] for payload in deltas] == [
        "same ",
        "answer",
        "same ",
        "answer",
    ]
