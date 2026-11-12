"""Real graphs through the SDK adapter: no mocked graph execution or state."""

from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from ksadk.interaction.native_events import NativeInteractionRequested, NativeSubmissionProgress
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, StartRequest
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from tests.interaction.test_langgraph_upgrade_semantics import EvenAnswer


class State(TypedDict, total=False):
    messages: list
    answer: object
    context: dict


async def start(tmp_path, node):
    calls = []

    def before(_state):
        calls.append("before")
        return {"context": {"private-marker": "never-chat"}}

    builder = StateGraph(State)
    builder.add_node("before", before)
    builder.add_node("wait", node)
    builder.add_edge(START, "before")
    builder.add_edge("before", "wait")
    builder.add_edge("wait", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    adapter = LangGraphRuntimeAdapter(runner)
    handle = await adapter.start(StartRequest(input="start", user_id="u", session_id="s"))
    events = [event async for event in adapter.stream(handle)]
    requests = [event for event in events if isinstance(event, NativeInteractionRequested)]
    assert len(requests) == 1, [(e.event_type, e.model_dump()) for e in events]
    assert "never-chat" not in str(
        [e.model_dump() for e in events if getattr(e, "item_kind", None) == "message"]
    )
    assert requests[0].interaction_id != requests[0]._native_target["call_id"]
    assert "task_id" not in requests[0].model_dump_json()
    return runner, adapter, handle, requests[0], calls


async def resume(adapter, handle, request, value, key):
    handle.native_ref["native_submission_key"] = key
    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=request._native_target["checkpoint_id"]),
        ResumePayload(kind="hitl_answer", call_id=request.interaction_id, data=value),
    )
    return [event async for event in adapter.stream(handle)]


async def test_sequential_identical_native_waits_have_distinct_public_occurrences(tmp_path):
    def node(_state):
        first = interrupt("Same")
        second = interrupt("Same")
        return {"answer": [first, second]}

    runner, adapter, handle, request, calls = await start(tmp_path, node)
    try:
        events = await resume(adapter, handle, request, False, "first")
        outcome = next(e for e in events if isinstance(e, NativeSubmissionProgress))
        assert outcome._outcomes[0].status == "consumed"
        second = next(e for e in events if isinstance(e, NativeInteractionRequested))
        assert second.interaction_id != request.interaction_id
        assert second._native_target["call_id"] == request._native_target["call_id"]
        events = await resume(adapter, handle, second, 0, "second")
        assert events[-1].event_type == "run.completed"
        assert calls == ["before"]
        assert not handle.native_ref["native_waits"]
        answers = [
            e.snapshot.parts[0].text
            for e in events
            if e.event_type == "item.completed" and e.item_kind == "message"
        ]
        assert answers == ["[False, 0]"]
    finally:
        await runner.close()


async def test_native_extra_validator_rejects_without_consuming_then_accepts_correction(tmp_path):
    tools = []

    def node(_state):
        answer = interrupt("Even amount", response_schema=EvenAnswer)
        tools.append(answer.amount)
        return {"answer": f"accepted {answer.amount}"}

    runner, adapter, handle, request, calls = await start(tmp_path, node)
    try:
        events = await resume(adapter, handle, request, {"amount": 3}, "bad")
        outcome = next(e for e in events if isinstance(e, NativeSubmissionProgress))
        assert outcome._outcomes[0].status == "rejected"
        assert request.interaction_id in handle.native_ref["native_waits"]
        assert not tools
        events = await resume(adapter, handle, request, {"amount": 0}, "corrected")
        assert events[-1].event_type == "run.completed"
        assert tools == [0] and calls == ["before"]
    finally:
        await runner.close()


async def test_parallel_partial_consumption_does_not_replay_successful_branch(tmp_path):
    class Parallel(TypedDict, total=False):
        messages: list
        left: int
        right: int
        answer: str

    executions = []

    def branch(name):
        def wait(_):
            answer = interrupt(name, response_schema=EvenAnswer)
            executions.append((name, answer.amount))
            return {name: answer.amount}

        return wait

    builder = StateGraph(Parallel)
    for name in ("left", "right"):
        builder.add_node(name, branch(name))
        builder.add_edge(START, name)
    builder.add_node("finish", lambda s: {"answer": f"{s['left']}+{s['right']}"})
    builder.add_edge(["left", "right"], "finish")
    builder.add_edge("finish", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    adapter = LangGraphRuntimeAdapter(runner)
    try:
        handle = await adapter.start(
            StartRequest(input="start", user_id="u", session_id="parallel")
        )
        events = [event async for event in adapter.stream(handle)]
        pending = {
            event.request.prompt: event
            for event in events
            if isinstance(event, NativeInteractionRequested)
        }
        assert set(pending) == {"left", "right"}
        checkpoint = pending["left"]._native_target["checkpoint_id"]
        await adapter.resume(
            handle,
            ResumeTarget(kind="checkpoint_id", id=checkpoint),
            ResumePayload(
                kind="hitl_answer",
                data={
                    pending["left"].interaction_id: {"amount": 2},
                    pending["right"].interaction_id: {"amount": 3},
                },
            ),
        )
        events = [event async for event in adapter.stream(handle)]
        progress = next(event for event in events if isinstance(event, NativeSubmissionProgress))
        assert {outcome.interaction_id: outcome.status for outcome in progress._outcomes} == {
            pending["left"].interaction_id: "consumed",
            pending["right"].interaction_id: "rejected",
        }
        remaining = [event for event in events if isinstance(event, NativeInteractionRequested)]
        assert [event.interaction_id for event in remaining] == [pending["right"].interaction_id]
        assert executions == [("left", 2)]
        events = await resume(adapter, handle, remaining[0], {"amount": 4}, "correction")
        assert events[-1].event_type == "run.completed"
        assert executions == [("left", 2), ("right", 4)]
    finally:
        await runner.close()


async def test_root_null_answer_is_consumed_by_the_canonical_runtime(tmp_path):
    from ksadk.interaction.langgraph import request_input

    received = []

    def node(_):
        received.append(request_input("JSON", {}))
        return {"answer": "done"}

    runner, adapter, handle, request, calls = await start(tmp_path, node)
    try:
        events = await resume(adapter, handle, request, None, "null")
        assert events[-1].event_type == "run.completed"
        assert received == [None]
        assert calls == ["before"]
    finally:
        await runner.close()
