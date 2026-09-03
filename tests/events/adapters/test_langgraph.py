"""LangGraph 1.2.9 raw v3 ProtocolEvent to canonical RuntimeEvent tests."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any, cast

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolCallTransformer, ToolNode
from langgraph.stream import AsyncGraphRunStream, UpdatesTransformer
from langgraph.types import interrupt

from ksadk.events.adapters.langgraph import (
    LangGraphAdapterContext,
    LangGraphEventAdapter,
    LangGraphMappingError,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RuntimeEvent,
    UsageReported,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.content import DataContent, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_item_id
from ksadk.events.reducer import StreamReducer


class _MultiBlockChatModel(FakeMessagesListChatModel):
    """Provider-shaped fake whose public model stream has two native blocks."""

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=[
                    {"type": "text", "text": "alpha", "index": 0},
                    {"type": "text", "text": "beta", "index": 1},
                ],
                chunk_position="last",
            )
        )


class _TypedBlockChatModel(FakeMessagesListChatModel):
    """Provider-shaped fake that emits the requested real protocol blocks."""

    blocks: list[dict[str, Any]]

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=self.blocks, chunk_position="last")
        )


def _context(
    *,
    checkpoint_ref: Mapping[str, str] | None = None,
) -> LangGraphAdapterContext:
    return LangGraphAdapterContext(
        run_id="runtime-run-7",
        graph_run_id="graph-run-7",
        initial_seq=10,
        checkpoint_ref=checkpoint_ref,
    )


def _one_model_graph(response: str = "hello") -> Any:
    model = FakeListChatModel(responses=[response])

    async def model_node(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    return builder.compile()


def _provided_model_graph(model: Any) -> Any:
    async def model_node(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    return builder.compile()


async def _capture(
    graph: Any,
    input_state: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    run = await graph.astream_events(input_state or {"messages": []}, version="v3")
    assert isinstance(run, AsyncGraphRunStream)
    return tuple([event async for event in run])


def _map_all(
    events: Iterable[Mapping[str, Any]],
    *,
    context: LangGraphAdapterContext | None = None,
) -> tuple[RuntimeEvent, ...]:
    adapter = LangGraphEventAdapter()
    ctx = context or _context()
    return tuple(
        canonical for event in events for canonical in adapter.map_protocol_event(event, ctx)
    )


def _completed(events: Iterable[RuntimeEvent]) -> list[ItemCompleted]:
    return [event for event in events if isinstance(event, ItemCompleted)]


def _message_protocol_events(
    events: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [event for event in events if event["method"] == "messages"]


@pytest.mark.asyncio
async def test_stream_run_consumes_public_async_graph_run_raw_log() -> None:
    """Break caught: canonical entry uses lossy `run.messages` handles."""

    run = await _one_model_graph().astream_events({"messages": []}, version="v3")
    assert isinstance(run, AsyncGraphRunStream)
    events = tuple([event async for event in LangGraphEventAdapter().stream_run(run, _context())])
    completed = [event for event in _completed(events) if event.item_kind == "message"]

    assert len(completed) == 1
    assert completed[0].snapshot.parts[0].text == "hello"
    assert completed[0].source.native_item_id
    assert completed[0].source.native_run_id
    assert completed[0].source.native_cursor
    assert run._exhausted is True
    await run.abort()
    assert run._exhausted is True


@pytest.mark.asyncio
async def test_stream_run_early_close_aborts_real_graph_run() -> None:
    """Break caught: closing the adapter leaves its source run alive."""

    run = await _one_model_graph().astream_events({"messages": []}, version="v3")
    assert isinstance(run, AsyncGraphRunStream)
    stream = LangGraphEventAdapter().stream_run(run, _context())
    await anext(stream)
    assert run._exhausted is False

    await stream.aclose()

    assert run._exhausted is True
    assert run._graph_aiter is None
    assert run._anext_task is None
    assert run._pumping is False


@pytest.mark.asyncio
async def test_real_root_and_nested_graph_events_keep_full_native_scope() -> None:
    """Break caught: deeper raw namespace is lost by message projection."""

    root_model = FakeListChatModel(responses=["same"])
    child_model = FakeListChatModel(responses=["same"])

    async def root_node(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await root_model.ainvoke(state["messages"])]}

    async def child_node(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await child_model.ainvoke(state["messages"])]}

    child = StateGraph(MessagesState)
    child.add_node("model", child_node)
    child.add_edge(START, "model")
    child.add_edge("model", END)
    outer = StateGraph(MessagesState)
    outer.add_node("root_model", root_node)
    outer.add_node("child", child.compile(name="child"))
    outer.add_edge(START, "root_model")
    outer.add_edge("root_model", "child")
    outer.add_edge("child", END)

    raw = _message_protocol_events(await _capture(outer.compile(name="outer")))
    starts = [event for event in raw if event["params"]["data"][0]["event"] == "message-start"]
    assert len(starts) == 2
    assert starts[0]["params"]["namespace"] == []
    assert len(starts[1]["params"]["namespace"]) == 1
    assert starts[1]["params"]["namespace"][0].startswith("child:")
    assert all(event["params"]["data"][1]["run_id"] for event in starts)
    assert starts[0]["params"]["data"][1]["run_id"] != starts[1]["params"]["data"][1]["run_id"]

    mapped = _map_all(raw)
    completed = _completed(mapped)
    assert len(completed) == 2
    assert len({event.scope_id for event in completed}) == 2
    assert len({event.item_id for event in completed}) == 2
    assert [event.snapshot.parts[0].text for event in completed] == ["same", "same"]


@pytest.mark.asyncio
async def test_real_two_llm_runs_same_text_remain_two_items_and_replay_stable() -> None:
    """Break caught: equal text merges two LLM run/message identities."""

    first_model = FakeListChatModel(responses=["same"])
    second_model = FakeListChatModel(responses=["same"])

    async def first(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await first_model.ainvoke(state["messages"])]}

    async def second(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await second_model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    raw = _message_protocol_events(await _capture(builder.compile()))

    mapped = _map_all(raw)
    replay = _map_all(raw)
    completed = _completed(mapped)
    assert len(completed) == 2
    assert len({event.scope_id for event in completed}) == 1
    assert len({event.item_id for event in completed}) == 2
    assert [event.snapshot.parts[0].text for event in completed] == ["same", "same"]
    assert [event.event_id for event in mapped] == [event.event_id for event in replay]
    assert all(event.schema_version == 2 for event in mapped)
    assert all(event.source.framework == "langgraph" for event in mapped)
    assert [
        dump_runtime_event(parse_runtime_event(dump_runtime_event(event))) for event in mapped
    ] == [dump_runtime_event(event) for event in mapped]


@pytest.mark.asyncio
async def test_real_multi_block_model_uses_native_block_indexes() -> None:
    """Break caught: block identity uses global event arrival order."""

    model = _MultiBlockChatModel(responses=[AIMessage(content="unused")])

    async def node(state: MessagesState) -> dict[str, list[BaseMessage]]:
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    mapped = _map_all(_message_protocol_events(await _capture(builder.compile())))
    updated = [event for event in mapped if isinstance(event, ItemUpdated)]
    completed = _completed(mapped)[0]

    append_updates = [event for event in updated if event.op == "append"]
    assert len({event.update.part_id for event in append_updates}) == 2
    assert tuple(part.part_id for part in completed.snapshot.parts) == tuple(
        event.update.part_id for event in append_updates
    )
    assert tuple(part.text for part in completed.snapshot.parts) == ("alpha", "beta")


@pytest.mark.asyncio
async def test_real_block_finish_repairs_dropped_delta_without_duplication() -> None:
    """Break caught: finalized block snapshot is appended as another delta."""

    raw = _message_protocol_events(await _capture(_one_model_graph("hello")))
    deltas = [
        event for event in raw if event["params"]["data"][0]["event"] == "content-block-delta"
    ]
    assert len(deltas) == 5
    dropped_seq = deltas[2]["seq"]
    mapped = _map_all(event for event in raw if event["seq"] != dropped_seq)
    updated = [event for event in mapped if isinstance(event, ItemUpdated)]
    completed = _completed(mapped)[0]

    assert updated[-1].op == "replace"
    assert updated[-1].event_id != completed.event_id
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    projection = reducer.snapshot()
    assert projection.items[0].status == "completed"
    assert projection.items[0].parts == (
        TextContent(part_id=updated[-1].update.part_id, text="hello"),
    )


@pytest.mark.asyncio
async def test_message_finish_rejects_a_block_without_native_finish() -> None:
    """Break caught: message-finish closes an unfinished block with empty text."""

    raw = _message_protocol_events(await _capture(_one_model_graph("hello")))
    without_block_finish = [
        event for event in raw if event["params"]["data"][0]["event"] != "content-block-finish"
    ]
    assert any(
        event["params"]["data"][0]["event"] == "message-finish" for event in without_block_finish
    )
    assert any(
        event["params"]["data"][0]["event"] == "content-block-delta"
        for event in without_block_finish
    )

    with pytest.raises(LangGraphMappingError) as caught:
        _map_all(without_block_finish)
    assert caught.value.code == "incomplete_content_block"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_native_type", ["content-block-delta", "content-block-finish"])
async def test_finished_block_rejects_late_mutation(late_native_type: str) -> None:
    """A finalized native block cannot publish another provisional mutation."""

    raw = _message_protocol_events(await _capture(_one_model_graph("hello")))
    finish_position = next(
        index
        for index, event in enumerate(raw)
        if event["params"]["data"][0]["event"] == "content-block-finish"
    )
    late = copy.deepcopy(
        next(event for event in raw if event["params"]["data"][0]["event"] == late_native_type)
    )
    mutated = [*raw[: finish_position + 1], late, *raw[finish_position + 1 :]]

    with pytest.raises(LangGraphMappingError) as caught:
        _map_all(mutated)
    assert caught.value.code == "content_block_already_finished"


@pytest.mark.asyncio
async def test_real_reasoning_and_text_blocks_are_distinct_typed_items() -> None:
    """Break caught: reasoning is appended as DataContent on a message item."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[
            {"type": "reasoning", "reasoning": "think", "index": 0},
            {"type": "text", "text": "answer", "index": 1},
        ],
    )
    mapped = _map_all(_message_protocol_events(await _capture(_provided_model_graph(model))))

    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    projection = reducer.snapshot()
    completed = [item for item in projection.items if item.status == "completed"]
    assert [(item.item_kind, item.phase) for item in completed] == [
        ("reasoning", "commentary"),
        ("message", "final_answer"),
    ]
    assert [item.parts[0].text for item in completed] == ["think", "answer"]
    assert len({item.item_id for item in completed}) == 2


@pytest.mark.asyncio
async def test_reasoning_start_without_optional_body_accumulates_delta() -> None:
    """ReasoningContentBlock.reasoning is optional on a legal native start."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[{"type": "reasoning", "reasoning": "think", "id": "r-1", "index": 0}],
    )
    raw = _message_protocol_events(await _capture(_provided_model_graph(model)))
    start = next(
        event for event in raw if event["params"]["data"][0]["event"] == "content-block-start"
    )
    start["params"]["data"][0]["content"].pop("reasoning", None)

    mapped = _map_all(raw)
    completed = next(event for event in _completed(mapped) if event.item_kind == "reasoning")
    assert completed.snapshot.parts[0].text == "think"


@pytest.mark.asyncio
async def test_reasoning_finish_without_optional_body_is_authoritative_empty_snapshot() -> None:
    """A legal finalized reasoning block without body authoritatively replaces deltas."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[{"type": "reasoning", "reasoning": "think", "id": "r-1", "index": 0}],
    )
    raw = _message_protocol_events(await _capture(_provided_model_graph(model)))
    finish = next(
        event for event in raw if event["params"]["data"][0]["event"] == "content-block-finish"
    )
    finish["params"]["data"][0]["content"].pop("reasoning", None)

    mapped = _map_all(raw)
    completed = next(event for event in _completed(mapped) if event.item_kind == "reasoning")
    assert completed.snapshot.parts[0].text == ""


@pytest.mark.asyncio
async def test_real_tool_call_block_is_an_independent_typed_item() -> None:
    """Break caught: a native tool call is flattened into message data."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[
            {
                "type": "tool_call_chunk",
                "id": "call-1",
                "name": "lookup",
                "args": '{"q":"x"}',
                "index": 0,
            }
        ],
    )
    mapped = _map_all(_message_protocol_events(await _capture(_provided_model_graph(model))))
    completed = [
        event
        for event in mapped
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    ]

    assert len(completed) == 1
    content = completed[0].snapshot.parts[0]
    assert isinstance(content, ToolCallContent)
    assert content.call_id == "call-1"
    assert content.name == "lookup"
    assert content.arguments == {"q": "x"}
    assert not any(
        isinstance(event, ItemCompleted) and event.item_kind == "message" for event in mapped
    )


@pytest.mark.asyncio
async def test_real_server_tool_call_and_result_are_distinct_typed_items() -> None:
    """Provider-side server results remain distinct from executable tool results."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[
            {
                "type": "server_tool_call",
                "id": "srv-1",
                "name": "web_search",
                "args": {"q": "x"},
                "index": 0,
            },
            {
                "type": "server_tool_result",
                "tool_call_id": "srv-1",
                "status": "success",
                "output": {"answer": "y"},
                "index": 1,
            },
        ],
    )
    raw = _message_protocol_events(await _capture(_provided_model_graph(model)))
    native = [event["params"]["data"][0] for event in raw]
    finished = [value["content"] for value in native if value["event"] == "content-block-finish"]
    assert [value["type"] for value in finished] == [
        "server_tool_call",
        "server_tool_result",
    ]

    completed = _completed(_map_all(raw))
    assert [event.item_kind for event in completed] == ["tool_call", "tool_result"]
    call = completed[0].snapshot.parts[0]
    result = completed[1].snapshot.parts[0]
    assert isinstance(call, ToolCallContent)
    assert call.call_id == "srv-1"
    assert call.name == "web_search"
    assert call.arguments == {"q": "x"}
    assert isinstance(result, ToolResultContent)
    assert result.call_id == "srv-1"
    assert result.result == {"answer": "y"}
    assert result.is_error is False
    assert result.part_id != call.part_id
    assert completed[1].source.metadata["tool_semantic"] == "provider_result"
    assert completed[1].item_id != stable_item_id(
        "langgraph", completed[1].scope_id, "tool_result", "srv-1"
    )


@pytest.mark.asyncio
async def test_real_server_tool_call_chunk_maps_tool_call() -> None:
    """The real chunk wire finalizes into the same typed tool-call lane."""

    model = _TypedBlockChatModel(
        responses=[AIMessage(content="unused")],
        blocks=[
            {
                "type": "server_tool_call_chunk",
                "id": "srv-c",
                "name": "code_exec",
                "args": '{"code":"x"}',
                "index": 0,
            }
        ],
    )
    raw = _message_protocol_events(await _capture(_provided_model_graph(model)))
    native = [event["params"]["data"][0] for event in raw]
    assert any(
        value["event"] == "content-block-start"
        and value["content"]["type"] == "server_tool_call_chunk"
        for value in native
    )
    assert any(
        value["event"] == "content-block-delta"
        and value["delta"]["fields"]["type"] == "server_tool_call_chunk"
        for value in native
    )

    completed = _completed(_map_all(raw))
    assert [event.item_kind for event in completed] == ["tool_call"]
    content = completed[0].snapshot.parts[0]
    assert isinstance(content, ToolCallContent)
    assert content.call_id == "srv-c"
    assert content.name == "code_exec"
    assert content.arguments == {"code": "x"}


@pytest.mark.asyncio
async def test_real_whole_message_raw_event_maps_as_terminal_snapshot() -> None:
    """Break caught: valid whole-AIMessage messages-channel fallback is rejected."""

    async def node(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(id="whole-msg", content="whole answer")]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    raw = _message_protocol_events(await _capture(builder.compile()))
    assert len(raw) == 1
    assert isinstance(raw[0]["params"]["data"][0], AIMessage)

    mapped = _map_all(raw)
    started = [event for event in mapped if isinstance(event, ItemStarted)]
    completed = _completed(mapped)
    assert len(started) == len(completed) == 1
    assert completed[0].source.native_item_id == "whole-msg"
    assert completed[0].snapshot.parts == (
        TextContent(part_id=completed[0].snapshot.parts[0].part_id, text="whole answer"),
    )


@pytest.mark.parametrize(
    "message_kwargs",
    [
        {
            "usage_metadata": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_token_details": {"cache_read": 3},
                "output_token_details": {"reasoning": 2},
            }
        },
        {
            "response_metadata": {
                "token_usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                }
            }
        },
    ],
)
def test_whole_ai_message_usage_metadata_maps_to_usage_reported(
    message_kwargs: Mapping[str, Any],
) -> None:
    message = AIMessage(id="usage-msg", content="answer", **message_kwargs)
    raw = {
        "type": "event",
        "method": "messages",
        "params": {
            "namespace": [],
            "timestamp": 1_786_439_000_000,
            "data": (message, {"langgraph_node": "model", "run_id": "llm-1"}),
        },
        "seq": 1,
    }

    mapped = LangGraphEventAdapter().map_protocol_event(raw, _context())
    usage = next(event for event in mapped if isinstance(event, UsageReported))
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (11, 7, 18)
    assert usage.cached_tokens == 3
    assert usage.reasoning_tokens == 2


@pytest.mark.asyncio
async def test_whole_tool_only_message_has_no_phantom_empty_final_answer() -> None:
    """Break caught: empty content creates a message beside a tool-only result."""

    async def node(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {
            "messages": [
                AIMessage(
                    id="whole-tool",
                    content="",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"q": "x"},
                            "id": "call-whole",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    builder = StateGraph(MessagesState)
    builder.add_node("model", node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    raw = _message_protocol_events(await _capture(builder.compile()))
    mapped = _map_all(raw)
    completed = _completed(mapped)

    assert [event.item_kind for event in completed] == ["tool_call"]
    assert isinstance(completed[0].snapshot.parts[0], ToolCallContent)


@pytest.mark.asyncio
async def test_whole_empty_message_without_tools_keeps_authoritative_snapshot() -> None:
    """An explicit empty AI message remains a real source-owned output item."""

    async def node(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(id="whole-empty", content="")]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    mapped = _map_all(_message_protocol_events(await _capture(builder.compile())))
    completed = _completed(mapped)

    assert [event.item_kind for event in completed] == ["message"]
    assert completed[0].snapshot.parts == ()


@pytest.mark.asyncio
async def test_stream_run_fails_closed_when_source_ends_with_open_message() -> None:
    """Break caught: truncated source silently leaves an open canonical item."""

    raw = _message_protocol_events(await _capture(_one_model_graph("hello")))
    truncated = raw[:-1]

    class _FiniteRun:
        aborted = False

        def __aiter__(self) -> AsyncIterator[Mapping[str, Any]]:
            async def events() -> AsyncIterator[Mapping[str, Any]]:
                for event in truncated:
                    yield event

            return events()

        async def abort(self) -> None:
            self.aborted = True

    adapter = LangGraphEventAdapter()
    run = _FiniteRun()
    with pytest.raises(LangGraphMappingError) as caught:
        tuple(
            [
                event
                async for event in adapter.stream_run(cast(AsyncGraphRunStream, run), _context())
            ]
        )
    assert caught.value.code == "open_messages_at_stream_end"
    assert run.aborted is True


@pytest.mark.asyncio
async def test_unknown_messages_data_event_fails_closed() -> None:
    """Break caught: a future typed mutation is silently discarded."""

    raw = _message_protocol_events(await _capture(_one_model_graph("hello")))
    adapter = LangGraphEventAdapter()
    adapter.map_protocol_event(raw[0], _context())
    unknown = copy.deepcopy(raw[1])
    _, metadata = unknown["params"]["data"]
    unknown["params"]["data"] = (
        {"event": "future-content-event"},
        metadata,
    )

    with pytest.raises(LangGraphMappingError) as caught:
        adapter.map_protocol_event(unknown, _context())
    assert caught.value.code == "unsupported_messages_event"


def test_custom_protocol_method_maps_lossless_typed_data() -> None:
    """Break caught: a legal v3 custom channel is silently discarded."""

    raw = {
        "type": "event",
        "method": "custom:metrics",
        "params": {
            "namespace": [],
            "timestamp": 1_786_439_000_000,
            "data": {"step": 2, "labels": ["a", "b"]},
        },
        "seq": 7,
    }
    mapped = LangGraphEventAdapter().map_protocol_event(raw, _context())
    completed = next(event for event in mapped if isinstance(event, ItemCompleted))
    content = completed.snapshot.parts[0]
    assert completed.item_kind == "data"
    assert isinstance(content, DataContent)
    assert content.data == {"step": 2, "labels": ["a", "b"]}
    assert completed.source.metadata["channel"] == "custom:metrics"


@pytest.mark.parametrize(
    "raw",
    [
        {
            "type": "event",
            "method": "custom:missing",
            "params": {"namespace": [], "timestamp": 1_786_439_000_000},
            "seq": 8,
        },
        {
            "type": "event",
            "method": "custom:invalid",
            "params": {
                "namespace": [],
                "timestamp": 1_786_439_000_000,
                "data": object(),
            },
            "seq": 9,
        },
        {
            "type": "event",
            "method": "values",
            "params": {
                "namespace": [],
                "timestamp": 1_786_439_000_000,
                "data": object(),
            },
            "seq": 10,
        },
    ],
)
def test_protocol_data_missing_or_not_stably_json_serializable_fails_closed(
    raw: Mapping[str, Any],
) -> None:
    """Break caught: invalid channel data is dropped or stringified."""

    with pytest.raises(LangGraphMappingError) as caught:
        LangGraphEventAdapter().map_protocol_event(raw, _context())
    assert caught.value.code in {"missing_protocol_data", "non_json_protocol_data"}


@pytest.mark.asyncio
async def test_real_tools_channel_maps_tool_result_with_native_call_id() -> None:
    """Break caught: v3 tools events are silently discarded."""

    @tool
    def lookup(q: str) -> str:
        """Return a deterministic lookup result."""

        return f"result:{q}"

    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([lookup]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile(transformers=[ToolCallTransformer])
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"q": "x"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )
    raw = [
        event for event in await _capture(graph, {"messages": [call]}) if event["method"] == "tools"
    ]
    assert [event["params"]["data"]["event"] for event in raw] == [
        "tool-started",
        "tool-finished",
    ]

    mapped = _map_all(raw)
    started = [
        event
        for event in mapped
        if isinstance(event, ItemStarted) and event.item_kind == "tool_result"
    ]
    completed = [
        event
        for event in mapped
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_result"
    ]
    assert len(started) == len(completed) == 1
    assert started[0].item_id == completed[0].item_id
    result = completed[0].snapshot.parts[0]
    assert isinstance(result, ToolResultContent)
    assert result.call_id == "call-1"
    assert result.result == "result:x"
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    assert reducer.snapshot().items[0].status == "completed"


@pytest.mark.asyncio
async def test_real_values_and_updates_channels_reduce_as_typed_data() -> None:
    """Break caught: graph state channels are flattened into message text."""

    async def node(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(id="state-msg", content="state value")]}

    builder = StateGraph(MessagesState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    graph = builder.compile(transformers=[UpdatesTransformer])
    raw = [event for event in await _capture(graph) if event["method"] in {"values", "updates"}]
    assert {event["method"] for event in raw} == {"values", "updates"}

    mapped = _map_all(raw)
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    projection = reducer.snapshot()
    assert projection.items
    assert all(item.item_kind == "data" for item in projection.items)
    assert all(item.status == "completed" for item in projection.items)
    assert all(isinstance(item.parts[0], DataContent) for item in projection.items)
    assert {event.source.metadata["channel"] for event in mapped} == {
        "values",
        "updates",
    }


@pytest.mark.asyncio
async def test_real_subgraph_lifecycle_is_scoped_without_closing_root_run() -> None:
    """Break caught: child completion is dropped or closes the root reducer."""

    async def child_node(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(id="child-msg", content="child output")]}

    child = StateGraph(MessagesState)
    child.add_node("work", child_node)
    child.add_edge(START, "work")
    child.add_edge("work", END)
    outer = StateGraph(MessagesState)
    outer.add_node("child", child.compile(name="child"))
    outer.add_edge(START, "child")
    outer.add_edge("child", END)
    lifecycle_raw = [
        event
        for event in await _capture(outer.compile(name="outer"))
        if event["method"] == "lifecycle"
    ]
    assert [event["params"]["data"]["event"] for event in lifecycle_raw] == [
        "started",
        "completed",
    ]
    assert lifecycle_raw[0]["params"]["namespace"] == []
    assert len(lifecycle_raw[0]["params"]["data"]["namespace"]) == 1

    mapped = _map_all(lifecycle_raw)
    progress = [event for event in mapped if isinstance(event, RunProgress)]
    status_started = [
        event for event in mapped if isinstance(event, ItemStarted) and event.item_kind == "status"
    ]
    status_completed = [
        event
        for event in mapped
        if isinstance(event, ItemCompleted) and event.item_kind == "status"
    ]
    assert len(progress) == 2
    assert len(status_started) == len(status_completed) == 1
    assert status_started[0].scope_id == status_completed[0].scope_id
    assert status_started[0].scope_id != progress[0].parent_scope_id
    assert not any(
        isinstance(event, (RunCompleted, RunFailed, RunCanceled, RunInterrupted))
        for event in mapped
    )
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    projection = reducer.snapshot()
    assert projection.status == "running"
    assert projection.items[0].status == "completed"


@pytest.mark.asyncio
async def test_real_failed_subgraph_is_scoped_item_failure_not_root_failure() -> None:
    """Break caught: child failure either disappears or terminates the root run."""

    async def fail(_state: MessagesState) -> dict[str, list[AIMessage]]:
        raise RuntimeError("child boom")

    child = StateGraph(MessagesState)
    child.add_node("fail", fail)
    child.add_edge(START, "fail")
    child.add_edge("fail", END)
    outer = StateGraph(MessagesState)
    outer.add_node("child", child.compile(name="child"))
    outer.add_edge(START, "child")
    outer.add_edge("child", END)
    run = await outer.compile(name="outer").astream_events({"messages": []}, version="v3")
    raw: list[Mapping[str, Any]] = []
    with pytest.raises(RuntimeError, match="child boom"):
        async for event in run:
            raw.append(event)
    lifecycle_raw = [event for event in raw if event["method"] == "lifecycle"]
    assert [event["params"]["data"]["event"] for event in lifecycle_raw] == [
        "started",
        "failed",
    ]

    mapped = _map_all(lifecycle_raw)
    failed = [event for event in mapped if isinstance(event, ItemFailed)]
    assert len(failed) == 1
    assert failed[0].item_kind == "status"
    assert failed[0].error.message == "child boom"
    assert not any(isinstance(event, RunFailed) for event in mapped)
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    projection = reducer.snapshot()
    assert projection.status == "running"
    assert projection.items[0].status == "failed"


@pytest.mark.asyncio
async def test_real_interrupt_preserves_actual_checkpoint_config() -> None:
    """Break caught: execution namespace is substituted for checkpoint_ns."""

    def ask(_state: MessagesState) -> dict[str, list[BaseMessage]]:
        interrupt("approve?")
        return {"messages": []}

    builder = StateGraph(MessagesState)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread-42"}}
    run = await graph.astream_events({"messages": []}, config, version="v3")
    raw = tuple([event async for event in run])
    interrupted_raw = next(event for event in raw if event["params"].get("interrupts"))
    state = await graph.aget_state(config)
    native_ref = state.config["configurable"]
    context = _context(
        checkpoint_ref={
            "thread_id": native_ref["thread_id"],
            "checkpoint_ns": native_ref["checkpoint_ns"],
            "checkpoint_id": native_ref["checkpoint_id"],
        }
    )

    mapped = LangGraphEventAdapter().map_protocol_event(interrupted_raw, context)
    interrupted = next(value for value in mapped if isinstance(value, RunInterrupted))
    continuation = next(value for value in mapped if isinstance(value, ContinuationCreated))
    assert interrupted.continuation_id == continuation.continuation_id
    assert continuation.continuation_kind == "graph_checkpoint"
    assert continuation.ref == {
        "thread_id": native_ref["thread_id"],
        "checkpoint_ns": native_ref["checkpoint_ns"],
        "checkpoint_id": native_ref["checkpoint_id"],
    }
    assert continuation.source.native_cursor == str(interrupted_raw["seq"])
    assert continuation.source.native_event_id is None
