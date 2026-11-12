"""Timeout recovery is reflected in the public AG-UI client stream."""

import asyncio
from types import SimpleNamespace

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langgraph.graph import END, START, MessagesState, StateGraph

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_shape", ["message", "output"])
async def test_public_client_replaces_canceled_partial_with_recovered_message(
    tmp_path, fallback_shape,
):
    deadline = asyncio.timeout(None)

    class State(MessagesState):
        output: str

    class StalledModel(FakeListChatModel):
        async def _astream(self, *args, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()

    model = StalledModel(responses=["unused"])

    async def chat(state):
        try:
            async with deadline:
                await model.ainvoke(state["messages"])
        except TimeoutError:
            if fallback_shape == "output":
                return {"output": "recovered"}
            return {"messages": [AIMessage(id="fallback", content="recovered")]}

    builder = StateGraph(State if fallback_shape == "output" else MessagesState)
    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    runner = LangGraphRunner(
        SimpleNamespace(entry_point="", agent_variable="", name="fixture",
                        type=SimpleNamespace(value="langgraph")),
        str(tmp_path),
    )
    runner._agent = builder.compile()
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: LangGraphRuntimeAdapter(runner))
    agent = KsadkAGUIAgent(
        name="fixture", executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=str(tmp_path)),
    )
    request = RunAgentInput(
        threadId="thread", runId="run", state={},
        messages=[UserMessage(id="u1", content="question")],
        tools=[], context=[], forwardedProps={},
    )
    try:
        events = [event async for event in agent.run(request)]
    finally:
        await runner.close()
    client_messages = {"history": "previous"}
    for event in events:
        if event.type == "TEXT_MESSAGE_CONTENT":
            client_messages[event.message_id] = (
                client_messages.get(event.message_id, "") + event.delta
            )
        elif event.type == "CUSTOM" and event.name == "ksadk.text_message_snapshot":
            client_messages[event.value["messageId"]] = event.value["content"]
    assert events[-1].type == "RUN_FINISHED" and events[-1].outcome.type == "success"
    assert client_messages == {
        "history": "previous",
        next(k for k in client_messages if k != "history"): "recovered",
    }
    assert not any(event.type == "MESSAGES_SNAPSHOT" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["model", "writer"])
async def test_public_multistep_recovery_selects_explicit_answer(tmp_path, prior):
    from tests.runners.test_langgraph_explicit_answer_contract import multi_step_recovery_graph

    runner = LangGraphRunner(
        SimpleNamespace(entry_point="", agent_variable="", name="fixture",
                        type=SimpleNamespace(value="langgraph")), str(tmp_path),
    )
    runner._agent = multi_step_recovery_graph(prior)
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: LangGraphRuntimeAdapter(runner))
    agent = KsadkAGUIAgent(
        name="fixture", executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=str(tmp_path)),
    )
    request = RunAgentInput(
        threadId="thread", runId="run", state={},
        messages=[UserMessage(id="u1", content="question")],
        tools=[], context=[], forwardedProps={},
    )
    try:
        events = [event async for event in agent.run(request)]
    finally:
        await runner.close()
    messages = {"history": "previous"}
    for event in events:
        if event.type == "TEXT_MESSAGE_CONTENT":
            messages[event.message_id] = messages.get(event.message_id, "") + event.delta
        elif event.type == "CUSTOM" and event.name == "ksadk.text_message_snapshot":
            messages[event.value["messageId"]] = event.value["content"]
    assert events[-1].type == "RUN_FINISHED" and events[-1].outcome.type == "success"
    assert messages == {"history": "previous", "run:assistant": "recovered"}
    assert not any(event.type == "MESSAGES_SNAPSHOT" for event in events)
