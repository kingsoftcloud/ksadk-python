from __future__ import annotations

import asyncio

import pytest

from ksadk.agents import (
    AgentEvent,
    EventType,
    LoopAgent,
    OrchestrationContext,
    ParallelAgent,
    RunnerAgent,
    SequentialAgent,
)


async def _collect_events(agent, context: OrchestrationContext) -> list[AgentEvent]:
    return [event async for event in agent.run_async(context)]


def _event_types(events: list[AgentEvent]) -> list[EventType]:
    return [event.event_type for event in events]


@pytest.mark.asyncio
async def test_sequential_agent_runs_sub_agents_in_order():
    async def research(context: OrchestrationContext) -> dict:
        return {
            "data": "research-notes",
            "state_delta": {"research": "research-notes"},
        }

    async def write(context: OrchestrationContext) -> dict:
        return {
            "data": f"draft:{context.get('research')}",
            "state_delta": {"draft": f"draft:{context.get('research')}"},
        }

    pipeline = SequentialAgent(name="pipeline", sub_agents=[research, write])
    context = OrchestrationContext(session_id="s1", state={"input": "topic"})

    events = await _collect_events(pipeline, context)

    assert context.state["research"] == "research-notes"
    assert context.state["draft"] == "draft:research-notes"
    assert [event.agent_name for event in events if event.event_type == EventType.TEXT_OUTPUT] == [
        "research",
        "write",
    ]
    assert _event_types(events).count(EventType.AGENT_START) >= 3
    assert _event_types(events).count(EventType.AGENT_END) >= 3


@pytest.mark.asyncio
async def test_parallel_agent_isolates_branch_context_and_merges_results():
    async def alpha(context: OrchestrationContext) -> dict:
        await asyncio.sleep(0.01)
        return {
            "data": "alpha-output",
            "state_delta": {"shared": "alpha", "alpha_only": 1},
        }

    async def beta(context: OrchestrationContext) -> dict:
        return {
            "data": "beta-output",
            "state_delta": {"shared": "beta", "beta_only": 2},
        }

    agent = ParallelAgent(name="fanout", sub_agents=[alpha, beta])
    context = OrchestrationContext(session_id="s1", state={"input": "topic", "seed": "base"})

    events = await _collect_events(agent, context)

    text_events = [event for event in events if event.event_type == EventType.TEXT_OUTPUT]
    assert {event.branch for event in text_events} == {"alpha", "beta"}
    assert context.state["seed"] == "base"
    assert context.state["alpha_only"] == 1
    assert context.state["beta_only"] == 2
    assert context.state["fanout_results"]["alpha"]["shared"] == "alpha"
    assert context.state["fanout_results"]["beta"]["shared"] == "beta"
    assert context.state["fanout_conflicts"]["shared"] == {"alpha": "alpha", "beta": "beta"}


@pytest.mark.asyncio
async def test_parallel_agent_does_not_leak_nested_state_between_branches():
    async def alpha(context: OrchestrationContext) -> dict:
        context.state["shared"]["items"].append("alpha")
        return {"state_delta": {"alpha_only": 1}}

    async def beta(context: OrchestrationContext) -> dict:
        context.state["shared"]["items"].append("beta")
        return {"state_delta": {"beta_only": 2}}

    agent = ParallelAgent(name="fanout", sub_agents=[alpha, beta])
    context = OrchestrationContext(state={"shared": {"items": []}})

    await _collect_events(agent, context)

    assert context.state["shared"] == {"items": []}
    assert context.state["fanout_results"]["alpha"]["shared"] == {"items": ["alpha"]}
    assert context.state["fanout_results"]["beta"]["shared"] == {"items": ["beta"]}
    assert context.state["fanout_conflicts"]["shared"] == {
        "alpha": {"items": ["alpha"]},
        "beta": {"items": ["beta"]},
    }


@pytest.mark.asyncio
async def test_parallel_agent_reraises_branch_failures():
    async def ok(context: OrchestrationContext) -> dict:
        return {"data": "ok", "state_delta": {"ok": True}}

    async def boom(context: OrchestrationContext) -> dict:
        raise RuntimeError("boom")

    agent = ParallelAgent(name="fanout", sub_agents=[ok, boom])
    context = OrchestrationContext()
    events: list[AgentEvent] = []

    with pytest.raises(RuntimeError, match="boom"):
        async for event in agent.run_async(context):
            events.append(event)

    assert any(
        event.agent_name == "boom"
        and event.event_type == EventType.ERROR
        and event.data == "boom"
        for event in events
    )
    assert any(
        event.agent_name == "fanout"
        and event.event_type == EventType.ERROR
        and event.data == "boom"
        for event in events
    )


@pytest.mark.asyncio
async def test_loop_agent_exits_on_max_iterations():
    async def tick(context: OrchestrationContext) -> dict:
        count = context.get("count", 0) + 1
        return {"data": count, "state_delta": {"count": count}}

    agent = LoopAgent(name="ticker", sub_agents=[tick], max_iterations=3)
    context = OrchestrationContext()

    events = await _collect_events(agent, context)

    assert context.state["count"] == 3
    assert [
        event.metadata["iteration"]
        for event in events
        if "iteration" in event.metadata
    ] == [0, 1, 2]


@pytest.mark.asyncio
async def test_loop_agent_exits_on_exit_condition():
    async def increment(context: OrchestrationContext) -> dict:
        count = context.get("count", 0) + 1
        return {"data": count, "state_delta": {"count": count}}

    agent = LoopAgent(
        name="until-two",
        sub_agents=[increment],
        exit_condition=lambda context: context.get("count", 0) >= 2,
        max_iterations=10,
    )
    context = OrchestrationContext()

    await _collect_events(agent, context)

    assert context.state["count"] == 2


@pytest.mark.asyncio
async def test_loop_agent_exits_on_escalate_event():
    async def review(context: OrchestrationContext) -> dict:
        count = context.get("count", 0) + 1
        return {
            "event_type": EventType.ESCALATE,
            "data": "needs-human",
            "state_delta": {"count": count},
            "escalate": True,
        }

    agent = LoopAgent(name="review-loop", sub_agents=[review], max_iterations=10)
    context = OrchestrationContext()

    events = await _collect_events(agent, context)

    assert context.state["count"] == 1
    assert any(event.event_type == EventType.ESCALATE for event in events)


@pytest.mark.asyncio
async def test_nested_orchestration_agents_share_state_through_parent_context():
    async def draft(context: OrchestrationContext) -> dict:
        return {"data": "draft", "state_delta": {"draft": "v1"}}

    async def review_a(context: OrchestrationContext) -> dict:
        return {"data": "review-a", "state_delta": {"score_a": 0.7}}

    async def review_b(context: OrchestrationContext) -> dict:
        return {"data": "review-b", "state_delta": {"score_b": 0.9}}

    parallel_reviews = ParallelAgent(name="reviews", sub_agents=[review_a, review_b])
    workflow = SequentialAgent(name="workflow", sub_agents=[draft, parallel_reviews])
    context = OrchestrationContext(state={"input": "topic"})

    await _collect_events(workflow, context)

    assert context.state["draft"] == "v1"
    assert context.state["score_a"] == 0.7
    assert context.state["score_b"] == 0.9
    assert context.state["reviews_results"]["review_a"]["score_a"] == 0.7
    assert context.state["reviews_results"]["review_b"]["score_b"] == 0.9


class _InvokeOnlyRunner:
    def __init__(self):
        self.calls = []

    async def invoke(self, input_data):
        self.calls.append(input_data)
        return {"output": f"invoke:{input_data['input']}", "state_delta": {"runner_mode": "invoke"}}


class _StreamingRunner:
    def __init__(self):
        self.calls = []

    async def invoke(self, input_data):
        self.calls.append(("invoke", input_data))
        return {"output": "unused"}

    async def stream(self, input_data):
        self.calls.append(("stream", input_data))
        yield {"delta": "hello", "type": "text"}
        yield {"delta": " world", "type": "text"}


@pytest.mark.asyncio
async def test_runner_adapter_supports_invoke_and_stream_modes():
    invoke_runner = _InvokeOnlyRunner()
    stream_runner = _StreamingRunner()
    agent = SequentialAgent(
        name="pipeline",
        sub_agents=[
            RunnerAgent(name="invoke_runner", runner=invoke_runner),
            stream_runner,
        ],
    )
    context = OrchestrationContext(session_id="session-1", state={"input": "hi"})

    events = await _collect_events(agent, context)

    assert invoke_runner.calls == [
        {"input": "hi", "state": {"input": "hi"}, "session_id": "session-1", "branch": ""}
    ]
    assert stream_runner.calls == [
        (
            "stream",
            {
                "input": "hi",
                "state": {
                    "input": "hi",
                    "runner_mode": "invoke",
                    "invoke_runner_output": "invoke:hi",
                },
                "session_id": "session-1",
                "branch": "",
            },
        )
    ]
    assert context.state["runner_mode"] == "invoke"
    assert context.state["invoke_runner_output"] == "invoke:hi"
    assert context.state["streaming_runner_output"] == "hello world"
    assert [
        event.data for event in events if event.agent_name == "streaming_runner"
    ] == ["hello", " world"]


@pytest.mark.asyncio
async def test_orchestration_agent_yields_error_event_before_reraising():
    async def ok(context: OrchestrationContext) -> dict:
        return {"data": "ok"}

    async def boom(context: OrchestrationContext) -> dict:
        raise RuntimeError("boom")

    agent = SequentialAgent(name="pipeline", sub_agents=[ok, boom])
    context = OrchestrationContext()
    events: list[AgentEvent] = []

    with pytest.raises(RuntimeError, match="boom"):
        async for event in agent.run_async(context):
            events.append(event)

    assert any(
        event.agent_name == "pipeline"
        and event.event_type == EventType.ERROR
        and event.data == "boom"
        for event in events
    )


def test_name_validation_rejects_invalid_and_duplicate_names():
    async def duplicate(context: OrchestrationContext) -> dict:
        return {"data": "one"}

    duplicate.__name__ = "same"

    async def also_duplicate(context: OrchestrationContext) -> dict:
        return {"data": "two"}

    also_duplicate.__name__ = "same"

    with pytest.raises(ValueError, match="valid identifier"):
        SequentialAgent(name="not valid", sub_agents=[])

    with pytest.raises(ValueError, match="unique"):
        SequentialAgent(name="pipeline", sub_agents=[duplicate, also_duplicate])

    with pytest.raises(ValueError, match="valid identifier"):
        SequentialAgent(name="pipeline", sub_agents=[lambda context: context])
