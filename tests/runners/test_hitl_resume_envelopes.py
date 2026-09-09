"""Native HITL resume values retain the identity of a selected interrupt."""

from types import SimpleNamespace
from typing import TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.runners.langgraph_runner import LangGraphRunner


class _State(TypedDict, total=False):
    a: dict
    b: dict


def _first_action(state):
    return {"a": interrupt({"tool_name": "first_action"})}


def _second_action(state):
    return {"b": interrupt({"tool_name": "second_action"})}


def _graph(*, parallel):
    builder = StateGraph(_State)
    builder.add_node("first", _first_action)
    builder.add_edge(START, "first")
    builder.add_edge("first", END)
    if parallel:
        builder.add_node("second", _second_action)
        builder.add_edge(START, "second")
        builder.add_edge("second", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["invoke", "stream"])
async def test_resume_envelope_targets_one_of_two_real_interrupts(entrypoint):
    graph = _graph(parallel=True)
    config = {"configurable": {"thread_id": "parallel"}}
    initial = await graph.ainvoke({}, config)
    pending = initial["__interrupt__"]
    target = next(item for item in pending if item.value["tool_name"] == "second_action")
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    runner._agent = graph
    value = {"decisions": [{"type": "approve"}]}
    payload = {
        "session_id": "parallel",
        "resume": True,
        "input": {"type": "ksadk_resume", "interrupt_id": target.id, "value": value},
    }
    if entrypoint == "invoke":
        await runner.invoke({**payload, "_ksadk_force_graph_invoke": True})
    else:
        _ = [event async for event in runner.stream(payload)]
    state = await graph.aget_state(config)
    assert state.values["b"] == value
    assert "a" not in state.values
    assert state.next == ("first",)


@pytest.mark.asyncio
async def test_single_interrupt_without_id_retains_value_resume():
    graph = _graph(parallel=False)
    config = {"configurable": {"thread_id": "single"}}
    await graph.ainvoke({}, config)
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    runner._agent = graph
    value = {"decisions": [{"type": "reject", "message": "do not execute"}]}
    result = await runner.invoke(
        {
            "_ksadk_force_graph_invoke": True,
            "session_id": "single",
            "resume": True,
            "input": {"type": "ksadk_resume", "value": value},
        }
    )
    assert result["raw"]["a"] == value


@pytest.mark.parametrize("kind", ["mcp_approval_response", "custom"])
def test_other_resume_protocols_are_unchanged(kind):
    envelope = {"type": kind, "interrupt_id": "first", "value": {"approve": True}}
    assert LangGraphRunner._unwrap_resume_value(envelope) is envelope


@pytest.mark.parametrize("key", ["interrupt_id", "approval_request_id", "id"])
def test_approval_response_alias_retains_target(key):
    value = {"decisions": [{"type": "approve"}]}
    assert LangGraphRunner._unwrap_resume_value(
        {
            "type": "ksadk.approval_response",
            key: "selected",
            "value": value,
        }
    ) == {"selected": value}
