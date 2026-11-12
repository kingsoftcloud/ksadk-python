"""The SDK-owned structured layout model is not a public business tool run."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import ensure_config
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from ksadk.compat.copilotkit_a2ui import _invoke_a2ui_subagent, build_ksadk_a2ui_tool
from ksadk.conversations.message_projection import project_session_messages
from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import ItemCompleted
from ksadk.events.canonical_store import runtime_event_to_session_event
from ksadk.events.content import ToolCallContent, ToolResultContent
from ksadk.runners.langgraph_runner import LangGraphRunner
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


@pytest.mark.asyncio
@pytest.mark.parametrize("async_model", [True, False])
@pytest.mark.parametrize("accepts_config", [True, False])
async def test_internal_model_invocation_explicitly_declares_visibility(
    async_model, accepts_config,
):
    captured = []
    response = SimpleNamespace(tool_calls=[{"name": "render_a2ui", "args": {"example": False}}])

    async def ainvoke(messages, *, config):
        captured.append(copy.deepcopy(config))
        assert messages[0].content == "Generate a layout"
        return response

    def invoke(messages, *, config):
        captured.append(copy.deepcopy(config))
        assert messages[0].content == "Generate a layout"
        return response

    async def old_ainvoke(messages):
        return await ainvoke(messages, config=ensure_config())

    def old_invoke(messages):
        return invoke(messages, config=ensure_config())

    method = (ainvoke if accepts_config else old_ainvoke) if async_model else (
        invoke if accepts_config else old_invoke
    )
    model = SimpleNamespace(**({"ainvoke": method} if async_model else {"invoke": method}))
    output = await _invoke_a2ui_subagent(
        model, prompt="Generate a layout", messages=[], timeout_seconds=1,
    )
    assert output == {"example": False}
    assert len(captured) == 1
    assert captured[0]["metadata"] == {
        "conversation_visibility": "internal",
        "ksadk_output_visibility": "internal",
        "ksadk_internal_operation": "a2ui_generation",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_kind", ["direct", "messages-only", "discarded-config"])
async def test_real_graph_keeps_public_generator_and_hides_only_its_internal_model(
    tmp_path, wrapper_kind,
):
    layout = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[{
        "name": "render_a2ui", "id": "internal-layout-call", "args": {
            "surfaceId": "generated", "data": {},
            "components": [{"id": "root", "component": "Text", "text": "Ready"}],
        },
    }])])
    public_model = SeqToolModel(msgs=[AIMessage(content="Public answer after generation.")])
    class OldWrapper:
        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            return await layout.ainvoke(messages)

    class DiscardingWrapper(OldWrapper):
        async def ainvoke(self, messages, **_kwargs):
            return await layout.ainvoke(messages)

    model = {"direct": layout, "messages-only": OldWrapper(),
             "discarded-config": DiscardingWrapper()}[wrapper_kind]
    generator = build_ksadk_a2ui_tool({"model": model})

    def prepare(_):
        return {"messages": [HumanMessage(content="Generate a card"), AIMessage(
            content="", tool_calls=[{
            "name": generator.name, "args": {"intent": "create"}, "id": "public-generator-call",
        }])]}

    async def publish(state):
        return {"messages": [await public_model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("prepare", prepare)
    builder.add_node("generate", ToolNode([generator]))
    builder.add_node("publish", publish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "generate")
    builder.add_edge("generate", "publish")
    builder.add_edge("publish", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    try:
        events = [event async for event in runner.stream_canonical_events({
            "input": "Generate a card", "session_id": "visibility-session",
            "run_id": "visibility-run",
        })]
    finally:
        await runner.close()
    assert events[-1].event_type == "run.completed"
    calls = [(event, part) for event in events if isinstance(event, ItemCompleted)
             for part in event.snapshot.parts if isinstance(part, ToolCallContent)]
    internal = [(event, part) for event, part in calls if part.call_id == "internal-layout-call"]
    # The production nostream tag suppresses this internal structured-output
    # call at the native boundary, including wrappers which discard config.
    assert internal == []
    public = [(event, part) for event, part in calls if part.call_id == "public-generator-call"]
    assert len(public) == 1 and project_conversation_item(public[0][0]).visibility == "public"
    results = [(event, part) for event in events if isinstance(event, ItemCompleted)
               for part in event.snapshot.parts if isinstance(part, ToolResultContent)]
    assert not any(part.call_id == "internal-layout-call" for _, part in results)
    envelope = next(part.result for _, part in results if part.call_id == "public-generator-call")
    assert json.loads(envelope)["a2ui_operations"]
    stored = []
    for index, event in enumerate(events, start=1):
        record = runtime_event_to_session_event(
            "visibility-session", event.model_copy(update={"seq": index}),
        )
        stored.append({"EventId": record.id, "EventType": record.event_type,
                       "Content": record.content, "Metadata": record.metadata,
                       "SeqId": record.seq_id, "InvocationId": record.invocation_id})
    history = project_session_messages(stored, include_tool_events=True)
    assert "internal-layout-call" not in json.dumps(history)
    assert "render_a2ui" not in json.dumps(history)
    assert generator.name in json.dumps(history)
    assert "Public answer after generation." in json.dumps(history)


@pytest.mark.asyncio
async def test_internal_model_config_does_not_mutate_parent_metadata():
    config = {"metadata": {"conversation_visibility": "public", "business_label": "sample"}}
    before = copy.deepcopy(config)

    class OldWrapper:
        async def ainvoke(self, messages):
            return SimpleNamespace(tool_calls=[{"name": "render_a2ui", "args": {"ok": True}}])

    async def parent(_value, config):
        metadata = copy.deepcopy(config["metadata"])
        assert await _invoke_a2ui_subagent(
            OldWrapper(), prompt="Generate", messages=[], timeout_seconds=1,
        ) == {"ok": True}
        assert config["metadata"] == metadata

    await RunnableLambda(parent).ainvoke({}, config=config)
    assert config == before


@pytest.mark.asyncio
@pytest.mark.parametrize("accepts_config", [True, False])
async def test_internal_type_error_never_reinvokes_the_model(accepts_config):
    calls = []

    async def configured(_messages, *, config):
        calls.append(config)
        raise TypeError("Example provider implementation error")

    async def old(_messages):
        return await configured(_messages, config=ensure_config())

    model = SimpleNamespace(ainvoke=configured if accepts_config else old)
    with pytest.raises(TypeError, match="Example provider implementation error"):
        await _invoke_a2ui_subagent(model, prompt="Generate", messages=[], timeout_seconds=1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_public_generator_failure_is_not_hidden_by_internal_model_metadata(tmp_path):
    class FailingModel(SeqToolModel):
        def _stream(self, *_args, **_kwargs):
            raise RuntimeError("Example layout provider failed")

    generator = build_ksadk_a2ui_tool({"model": FailingModel(msgs=[])})
    builder = StateGraph(MessagesState)
    builder.add_node("prepare", lambda _: {"messages": [AIMessage(content="", tool_calls=[{
        "name": generator.name, "id": "failed-public-generator", "args": {},
    }])]})
    builder.add_node("generate", ToolNode([generator]))
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "generate")
    builder.add_edge("generate", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    try:
        events = [event async for event in runner.stream_canonical_events({
            "input": "Generate", "session_id": "failure-session", "run_id": "failure-run",
        })]
    finally:
        await runner.close()
    results = [(event, part) for event in events if isinstance(event, ItemCompleted)
               for part in event.snapshot.parts if isinstance(part, ToolResultContent)]
    public = [(event, part) for event, part in results if part.call_id == "failed-public-generator"]
    assert len(public) == 1
    assert project_conversation_item(public[0][0]).visibility == "public"
    assert "Example layout provider failed" in public[0][1].result
    assert events[-1].event_type == "run.completed"
