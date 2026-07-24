from types import SimpleNamespace
from typing import TypedDict

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


class _State(TypedDict, total=False):
    output: str


def _graph():
    def gate(_state):
        decision = interrupt({"message": "Approve the action?"})
        return {"output": f"decision:{decision}"}

    builder = StateGraph(_State)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=MemorySaver())


def _input(run_id: str, resume=None):
    return RunAgentInput(
        threadId="stable-thread",
        runId=run_id,
        state={},
        messages=[UserMessage(id="u1", content="please do it")],
        tools=[],
        context=[],
        forwardedProps={},
        resume=resume,
    )


@pytest.mark.asyncio
async def test_langgraph_interrupt_resumes_same_thread_with_command_resume():
    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="",
            agent_variable="",
            name="fixture",
            type=SimpleNamespace(value="langgraph"),
        ),
        ".",
    )
    runner._agent = _graph()  # test fixture; production adapter never reads this attribute
    agent = KsadkAGUIAgent(name="fixture", adapter=LangGraphRuntimeAdapter(runner))

    first = [event async for event in agent.run(_input("run-1"))]
    finished = first[-1]
    assert finished.type.value == "RUN_FINISHED"
    assert finished.outcome.type == "interrupt"
    interrupt_id = finished.outcome.interrupts[0].id

    resumed = [
        event
        async for event in agent.run(
            _input(
                "run-2",
                resume=[
                    {
                        "interruptId": interrupt_id,
                        "status": "resolved",
                        "payload": {"approved": True},
                    }
                ],
            )
        )
    ]
    assert resumed[-1].type.value == "RUN_FINISHED"
    assert resumed[-1].outcome.type == "success"
    assert any(getattr(event, "delta", "") == "decision:{'approved': True}" for event in resumed)
