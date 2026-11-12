"""Real graphs keep intermediate model text separate from declared answers."""

import asyncio
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from ksadk.agui._agent_types import _ThreadRun, _WireState
from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import (
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    UsageReported,
)
from ksadk.events.content import TextContent
from ksadk.events.reducer import StreamReducer
from ksadk.runners._langgraph_answer_contract import uses_explicit_answer_contract
from ksadk.runners._langgraph_output import extract_output
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RunHandle


async def collect(graph, tmp_path):
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = graph
    try:
        return [
            event
            async for event in runner.stream_canonical_events(
                {
                    "input": "question",
                    "session_id": "answer-contract",
                    "run_id": "run-answer",
                }
            )
        ]
    finally:
        await runner.close()


async def agui_text(events):
    agent = KsadkAGUIAgent(name="test", _shared=SimpleNamespace())
    wire = _WireState("answer-contract", "run-answer", "message", "reasoning")
    run = _ThreadRun(
        RunHandle(run_id="run-answer", session_id="answer-contract", runtime_type="langgraph")
    )
    text = []
    for event in events:
        if getattr(event, "item_kind", None) != "message":
            continue
        async for projected in agent._project(event, wire, run):
            if projected.type == "TEXT_MESSAGE_CONTENT":
                text.append(projected.delta)
    return "".join(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("catch_timeout", [True, False])
async def test_streamed_model_timeout_preserves_graph_recovery(tmp_path, catch_timeout):
    class StalledModel(FakeListChatModel):
        async def _astream(self, *args, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            # Expire the budget only after the partial chunk reached LangGraph.
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()

    model = StalledModel(responses=["unused"])

    async def chat(state):
        try:
            async with deadline:
                await model.ainvoke(state["messages"])
        except TimeoutError:
            if not catch_timeout:
                raise
            return {"messages": [AIMessage(id="fallback", content="recovered")]}

    deadline = asyncio.timeout(None)
    graph = StateGraph(MessagesState)
    graph.add_node("chat", chat)
    graph.add_edge(START, "chat")
    graph.add_edge("chat", END)
    events = await collect(graph.compile(), tmp_path)
    assert any(
        isinstance(event, ItemUpdated)
        and event.item_kind == "message"
        and event.update.text == "partial"
        for event in events
    )
    assert not any(isinstance(event, UsageReported) for event in events)
    if not catch_timeout:
        assert events[-1].event_type == "run.failed"
        return

    failed = [event for event in events if isinstance(event, ItemFailed)]
    assert len(failed) == 1
    assert failed[0].item_kind == "message"
    assert failed[0].error.code == "langgraph_model_stream_canceled"
    assert events[-1].event_type == "run.completed"
    reducer = StreamReducer()
    for seq, event in enumerate(events, 1):
        reducer.apply(event.model_copy(update={"seq": seq}))
    failed_item = next(
        item for item in reducer.snapshot().items
        if item.item_id == failed[0].item_id and item.scope_id == failed[0].scope_id
    )
    assert failed_item.status == "failed"
    assert failed_item.error.code == "langgraph_model_stream_canceled"
    assert [part.text for part in failed_item.parts if isinstance(part, TextContent)] == ["partial"]
    completed = next(
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.source.native_item_id == "fallback"
    )
    assert [ref.item_id for ref in events[-1].output_refs] == [completed.item_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("output", '{"resources":[{"id":"public-resource"}],"ok":true}'),
        ("answer", {"resources": [], "ok": False}),
        ("answer", 0),
    ],
)
async def test_explicit_output_contract_suppresses_model_intermediates_and_publishes_answer(
    tmp_path,
    field,
    value,
):
    State = TypedDict("ExplicitAnswer", {field: object, "private_context": str}, total=False)
    private = '{"classification":"internal-selector","context":"not-an-answer"}'
    model = FakeListChatModel(responses=[private])

    @tool
    def lookup() -> str:
        """Return a visible ordinary business JSON tool result."""
        return '{"tool_result":"kept-public"}'

    async def select(_state):
        response = await model.ainvoke("classify this request")
        await lookup.ainvoke({})
        return {"private_context": response.content}

    child = StateGraph(State)
    child.add_node("select", select)
    child.add_edge(START, "select")
    child.add_edge("select", END)
    graph = StateGraph(State)
    graph.add_node("child", child.compile())
    graph.add_node("publish", lambda _: {field: value})
    graph.add_edge(START, "child")
    graph.add_edge("child", "publish")
    graph.add_edge("publish", END)
    compiled = graph.compile()
    assert uses_explicit_answer_contract(compiled)
    events = await collect(compiled, tmp_path)
    assert events[-1].event_type == "run.completed"
    intermediate = [
        event
        for event in events
        if getattr(event, "item_kind", None) == "message"
        and event.source.metadata.get("channel") == "messages"
    ]
    assert intermediate and any(isinstance(event, ItemUpdated) for event in intermediate)
    assert all(
        event.source.metadata["conversation_visibility"] == "internal" for event in intermediate
    )
    assert all(
        event.phase == "commentary" for event in intermediate if isinstance(event, ItemStarted)
    )
    assert all(project_conversation_item(event).visibility == "internal" for event in intermediate)
    public = [
        event
        for event in events
        if isinstance(event, ItemCompleted)
        and event.item_kind == "message"
        and project_conversation_item(event).visibility == "public"
    ]
    expected = extract_output({field: value})
    assert len(public) == 1
    assert public[0].parent_scope_id is None
    assert public[0].snapshot.parts == (TextContent(part_id="answer", text=expected),)
    assert await agui_text(events) == expected
    assert private not in await agui_text(events)
    tools = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_result"
    ]
    assert len(tools) == 1 and tools[0].snapshot.parts[0].result == '{"tool_result":"kept-public"}'
    assert project_conversation_item(tools[0]).visibility == "public"
    assert events[-1].output_refs[0].item_id == public[0].item_id


@pytest.mark.asyncio
async def test_messages_state_retains_public_streamed_business_json(tmp_path, monkeypatch):
    expected = '{"resources":[{"id":"ordinary-json"}],"ok":true}'
    model = FakeListChatModel(responses=[expected])

    async def chat(state):
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    graph = builder.compile()
    assert not uses_explicit_answer_contract(graph)
    rows = await collect(graph, tmp_path)
    deltas = [
        event for event in rows if isinstance(event, ItemUpdated) and event.item_kind == "message"
    ]
    assert deltas and all(
        project_conversation_item(event).visibility == "public" for event in deltas
    )
    assert rows.index(deltas[0]) < len(rows) - 1
    assert rows[-1].event_type == "run.completed"
    # Native v3 can emit a complete replace after token appends. Preserve that
    # existing stream contract rather than treating every delta as an append.
    assert all(event.update.text in expected or event.update.text == expected for event in deltas)
    visible_text = await agui_text(rows)
    assert visible_text == expected
    assert not any(
        event.source.metadata.get("conversation_visibility") == "internal" for event in deltas
    )
    # Disable the new contract selector to exercise the preceding path. This
    # proves the existing replace/replay text sequence was not changed here.
    monkeypatch.setattr(
        "ksadk.runners._langgraph_canonical_stream.uses_explicit_answer_contract", lambda _: False
    )
    baseline = await collect(graph, tmp_path)
    assert await agui_text(baseline) == visible_text
    baseline_deltas = [
        event
        for event in baseline
        if isinstance(event, ItemUpdated) and event.item_kind == "message"
    ]
    assert [(event.op, event.update.text) for event in baseline_deltas] == [
        (event.op, event.update.text) for event in deltas
    ]


def test_unknown_output_schemas_and_messages_contract_keep_existing_streaming():
    def schema(properties):
        return SimpleNamespace(
            get_output_schema=lambda: SimpleNamespace(
                model_json_schema=lambda: {"properties": properties}
            )
        )

    def unavailable():
        raise ValueError("custom schema cannot be exported")

    assert not uses_explicit_answer_contract(SimpleNamespace())
    assert not uses_explicit_answer_contract(SimpleNamespace(get_output_schema=unavailable))
    assert not uses_explicit_answer_contract(schema(None))
    assert not uses_explicit_answer_contract(schema({"private_context": {}}))
    assert not uses_explicit_answer_contract(schema({"output": {}, "messages": {}}))
    assert uses_explicit_answer_contract(schema({"answer": {}}))


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"visibility": "internal"}, "internal"),
        ({"visibility": "hidden"}, "hidden"),
        ({"visibility": "hidden", "conversation_visibility": "internal"}, "internal"),
        ({}, "public"),
    ],
)
def test_known_message_kind_respects_authoritative_visibility(metadata, expected):
    from ksadk.events.canonical import SourceRef
    from ksadk.events.content import ContentSnapshot

    event = ItemCompleted(
        schema_version=2,
        event_id="visibility",
        seq=1,
        timestamp=1,
        run_id="run",
        scope_id="scope",
        source=SourceRef(framework="langgraph", metadata=metadata),
        item_id="message",
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text='{"business":true}'),)),
    )
    item = project_conversation_item(event)
    assert item.visibility == expected
    assert item.payload["text"] == '{"business":true}'


@pytest.mark.asyncio
async def test_explicit_output_recovers_after_canceled_model_message(tmp_path):
    class State(MessagesState):
        output: str

    deadline = asyncio.timeout(None)

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
            return {"output": "recovered"}

    graph = StateGraph(State)
    graph.add_node("chat", chat)
    graph.add_edge(START, "chat")
    graph.add_edge("chat", END)
    events = await collect(graph.compile(), tmp_path)
    failed = next(event for event in events if isinstance(event, ItemFailed))
    assert failed.error.code == "langgraph_model_stream_canceled"
    reducer = StreamReducer()
    for seq, event in enumerate(events, 1):
        reducer.apply(event.model_copy(update={"seq": seq}))
    failed_item = next(
        item for item in reducer.snapshot().items
        if item.item_id == failed.item_id and item.scope_id == failed.scope_id
    )
    assert failed_item.status == "failed"
    assert failed_item.error.code == "langgraph_model_stream_canceled"
    assert [part.text for part in failed_item.parts if isinstance(part, TextContent)] == ["partial"]
    assert any(isinstance(event, ItemUpdated) and event.item_id == failed.item_id
               and isinstance(event.update, TextContent) and event.update.text == "partial"
               for event in events)
    assert events[-1].event_type == "run.completed"
    refs = {(ref.scope_id, ref.item_id) for ref in events[-1].output_refs}
    answers = [event for event in events if isinstance(event, ItemCompleted)
               and (event.scope_id, event.item_id) in refs]
    assert [part.text for answer in answers for part in answer.snapshot.parts
            if isinstance(part, TextContent)] == ["recovered"]


def multi_step_recovery_graph(prior, outcome="recover"):
    from langgraph.config import get_stream_writer

    class State(MessagesState):
        output: str

    deadline = asyncio.timeout(None)
    good = FakeListChatModel(responses=["answer"])

    class StalledModel(FakeListChatModel):
        async def _astream(self, *args, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()

    stalled = StalledModel(responses=["unused"])

    async def first(state):
        if prior == "writer":
            get_stream_writer()("checking...")
            return {}
        return {"messages": [await good.ainvoke(state["messages"])]}

    async def second(state):
        if outcome == "success":
            return {"output": "different explicit answer"}
        try:
            async with deadline:
                await stalled.ainvoke(state["messages"])
        except TimeoutError:
            if outcome == "unhandled":
                raise
            return {"output": "recovered"} if outcome == "recover" else {}

    builder = StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["model", "writer"])
async def test_multistep_recovery_refs_select_explicit_answer_and_keep_trace(tmp_path, prior):
    events = await collect(multi_step_recovery_graph(prior), tmp_path)
    reducer = StreamReducer()
    for seq, event in enumerate(events, 1):
        reducer.apply(event.model_copy(update={"seq": seq}))
    projection = reducer.snapshot()
    failed = next(item for item in projection.items if item.status == "failed")
    assert failed.error.code == "langgraph_model_stream_canceled"
    assert [part.text for part in failed.parts if isinstance(part, TextContent)] == ["partial"]
    assert any(item.status == "completed" and any(
        isinstance(part, TextContent)
        and part.text == ("answer" if prior == "model" else "checking...")
        for part in item.parts
    ) for item in projection.items)
    refs = {(ref.scope_id, ref.item_id) for ref in events[-1].output_refs}
    assert events[-1].event_type == "run.completed"
    assert len(refs) == 1
    assert [part.text for item in projection.items if (item.scope_id, item.item_id) in refs
            for part in item.parts if isinstance(part, TextContent)] == ["recovered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "no_output", "unhandled"])
async def test_multistep_controls_preserve_answer_priority_and_failures(tmp_path, outcome):
    events = await collect(multi_step_recovery_graph("model", outcome), tmp_path)
    if outcome == "unhandled":
        assert events[-1].event_type == "run.failed"
        return
    refs = {(ref.scope_id, ref.item_id) for ref in events[-1].output_refs}
    assert [part.text for event in events if isinstance(event, ItemCompleted)
            and (event.scope_id, event.item_id) in refs
            for part in event.snapshot.parts if isinstance(part, TextContent)] == ["answer"]


@pytest.mark.asyncio
async def test_parallel_canceled_message_keeps_good_branch_output(tmp_path):
    class State(MessagesState):
        output: str

    deadline = asyncio.timeout(None)
    good = FakeListChatModel(responses=["good"])

    class StalledModel(FakeListChatModel):
        async def _astream(self, *args, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()

    stalled = StalledModel(responses=["unused"])

    async def canceled(state):
        try:
            async with deadline:
                await stalled.ainvoke(state["messages"])
        except TimeoutError:
            return {}

    async def completed(state):
        return {"messages": [await good.ainvoke(state["messages"])]}

    graph = StateGraph(State)
    graph.add_node("canceled", canceled)
    graph.add_node("completed", completed)
    graph.add_edge(START, "canceled")
    graph.add_edge(START, "completed")
    graph.add_edge("canceled", END)
    graph.add_edge("completed", END)
    events = await collect(graph.compile(), tmp_path)
    assert events[-1].event_type == "run.completed"
    assert any(isinstance(event, ItemFailed) for event in events)
    refs = {(ref.scope_id, ref.item_id) for ref in events[-1].output_refs}
    assert [part.text for event in events if isinstance(event, ItemCompleted)
            and (event.scope_id, event.item_id) in refs
            for part in event.snapshot.parts if isinstance(part, TextContent)] == ["good"]
