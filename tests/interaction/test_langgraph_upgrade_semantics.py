"""Real upstream contracts required before enabling the upgraded resume path.

These graphs deliberately do not use SDK adapters: a passing adapter mock cannot
prove the framework accepts a corrected answer or consumes parallel answers.
"""

from __future__ import annotations

import asyncio
from importlib.metadata import version

import pytest
from packaging.version import Version
from pydantic import BaseModel, ValidationError, field_validator
from typing_extensions import TypedDict

pytestmark = pytest.mark.skipif(
    Version(version("langgraph")) < Version("1.2.15"),
    reason="Upgrade gate requires LangGraph 1.2.15 public typed-interrupt APIs",
)


class State(TypedDict, total=False):
    answer: object
    left: object
    right: object


class EvenAnswer(BaseModel):
    amount: int
    enabled: bool = False
    note: str | None = None

    @field_validator("amount")
    @classmethod
    def even(cls, value: int) -> int:
        if value % 2:
            raise ValueError("amount must be even")
        return value


def build(node, saver):
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(State)
    builder.add_node("wait", node)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    return builder.compile(checkpointer=saver)


@pytest.mark.asyncio
async def test_native_validator_rejection_is_correctable_without_running_tool():
    from langgraph.callbacks import GraphCallbackHandler
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.errors import is_invalid_resume
    from langgraph.types import Command, interrupt

    resumes = []
    tool_calls = []

    class Observe(GraphCallbackHandler):
        def on_resume(self, event):
            resumes.append(event.checkpoint_id)

    def node(_state):
        answer = interrupt("Fill parameters", response_schema=EvenAnswer)
        tool_calls.append(answer.amount)
        return {"answer": answer.model_dump()}

    graph = build(node, InMemorySaver())
    config = {"configurable": {"thread_id": "gate-typed"}, "callbacks": [Observe()]}
    pending = (await graph.ainvoke({}, config))["__interrupt__"][0]
    assert pending.response_schema == EvenAnswer.model_json_schema()
    with pytest.raises(ValidationError) as rejected:
        await graph.ainvoke(Command(resume={pending.id: {"amount": 3}}), config)
    assert is_invalid_resume(rejected.value)
    assert resumes  # on_resume precedes validation; it is NOT consumption proof.
    assert tool_calls == []
    assert [item.id for item in (await graph.aget_state(config)).interrupts] == [pending.id]
    result = await graph.ainvoke(
        Command(resume={pending.id: {"amount": 0, "enabled": False, "note": None}}),
        config,
    )
    assert result["answer"] == {"amount": 0, "enabled": False, "note": None}
    assert tool_calls == [0]
    assert not (await graph.aget_state(config)).next


@pytest.mark.asyncio
async def test_same_node_same_prompt_reuses_id_but_has_two_consumptions():
    from langgraph.callbacks import GraphCallbackHandler
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command, interrupt

    task_events = []

    class Observe(GraphCallbackHandler):
        def on_chain_error(self, error, *, run_id, **_kwargs):
            if isinstance(error, GraphInterrupt):
                task_events.append(str(run_id))

    def node(_state):
        first = interrupt("Same question")
        second = interrupt("Same question")
        return {"answer": [first, second]}

    graph = build(node, InMemorySaver())
    config = {"configurable": {"thread_id": "gate-sequential"}, "callbacks": [Observe()]}
    first = (await graph.ainvoke({}, config))["__interrupt__"][0]
    before = await graph.aget_state(config)
    second = (await graph.ainvoke(Command(resume={first.id: False}), config))["__interrupt__"][0]
    after = await graph.aget_state(config)
    assert first.id == second.id
    assert first.value == second.value
    # Even the checkpoint can remain unchanged between two waits in one task.
    # A public occurrence must advance from execution/consumption evidence,
    # never by comparing the native id, question text or checkpoint id.
    assert before.config == after.config
    assert len(task_events) == 2
    final = await graph.ainvoke(Command(resume={second.id: 0}), config)
    assert final["answer"] == [False, 0]


@pytest.mark.asyncio
async def test_parallel_native_rejection_keeps_successful_branch_result():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    calls = []

    def left(_state):
        answer = interrupt("Left", response_schema=EvenAnswer)
        calls.append(("left", answer.amount))
        return {"left": answer.amount}

    def right(_state):
        answer = interrupt("Right", response_schema=EvenAnswer)
        calls.append(("right", answer.amount))
        return {"right": answer.amount}

    builder = StateGraph(State)
    for name, node in (("left", left), ("right", right)):
        builder.add_node(name, node)
        builder.add_edge(START, name)
        builder.add_edge(name, END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "gate-parallel"}}
    pending = (await graph.ainvoke({}, config))["__interrupt__"]
    ids = {item.value: item.id for item in pending}
    with pytest.raises(ValidationError):
        await graph.ainvoke(
            Command(resume={ids["Left"]: {"amount": 2}, ids["Right"]: {"amount": 3}}),
            config,
        )
    snapshot = await graph.aget_state(config)
    successful = next(task for task in snapshot.tasks if task.name == "left")
    assert successful.result == {"left": 2}
    assert calls == [("left", 2)]
    result = await graph.ainvoke(Command(resume={ids["Right"]: {"amount": 4}}), config)
    assert result["left"] == 2 and result["right"] == 4
    assert calls == [("left", 2), ("right", 4)]


@pytest.mark.asyncio
async def test_sqlite_reopen_preserves_target_and_root_null(tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command, interrupt

    def node(_state):
        return {"answer": interrupt("Optional", response_schema={"type": ["string", "null"]})}

    path = str(tmp_path / "checkpoints.sqlite")
    config = {"configurable": {"thread_id": "gate-persistent"}}
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = build(node, saver)
        pending = (await graph.ainvoke({}, config))["__interrupt__"][0]
        checkpoint_id = (await graph.aget_state(config)).config["configurable"]["checkpoint_id"]
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = build(node, saver)
        snapshot = await graph.aget_state(config)
        assert snapshot.config["configurable"]["checkpoint_id"] == checkpoint_id
        assert snapshot.interrupts[0].id == pending.id
        result = await graph.ainvoke(Command(resume={pending.id: None}), config)
        assert "answer" in result and result["answer"] is None
        assert not (await graph.aget_state(config)).interrupts


@pytest.mark.asyncio
async def test_subgraph_target_is_readable_after_slow_parent_checkpoint():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    delayed = asyncio.Event()
    release = asyncio.Event()

    class SlowSaver(InMemorySaver):
        async def aput(self, config, checkpoint, metadata, new_versions):
            if not config["configurable"].get("checkpoint_ns") and metadata.get("step") == 0:
                delayed.set()
                await release.wait()
            return await super().aput(config, checkpoint, metadata, new_versions)

    child = build(lambda _state: {"answer": interrupt("Child")}, None)
    parent = StateGraph(State)
    parent.add_node("child", child)
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    graph = parent.compile(checkpointer=SlowSaver())
    config = {"configurable": {"thread_id": "gate-delayed-parent"}}
    task = asyncio.create_task(graph.ainvoke({}, config))
    try:
        await asyncio.wait_for(delayed.wait(), timeout=5)
        assert not task.done()
    finally:
        release.set()
    result = await asyncio.wait_for(task, timeout=5)
    snapshot = await graph.aget_state(config, subgraphs=True)
    assert snapshot.config["configurable"]["checkpoint_id"]
    assert snapshot.interrupts[0].id == result["__interrupt__"][0].id
    assert snapshot.tasks[0].state.config["configurable"]["checkpoint_ns"].startswith("child:")
    # Resume the parent graph with its original address. Passing the child's
    # state-reader namespace to the parent invocation starts the wrong scope.
    resumed = await graph.ainvoke(Command(resume={snapshot.interrupts[0].id: False}), snapshot.config)
    assert resumed["answer"] is False
