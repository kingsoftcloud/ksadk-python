"""Real LangChain tool execution must publish a passive card before stream EOF."""

import json
from types import SimpleNamespace

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.reducer import StreamReducer
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.sessions.local_service import LocalSessionService
from tests.a2ui.test_official_wire import components, create
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


def test_generated_surface_is_promoted_to_native_input_without_recreating_it(tmp_path):
    from ksadk.events.canonical import SourceRef, StructuredInputRequest
    from ksadk.interaction.native_events import NativeInteractionRequested
    from ksadk.runtime.adapter import RunHandle
    from ksadk.runtime._runner_adapter.a2ui_surface import _a2ui_surface_event

    adapter = LangGraphRuntimeAdapter(LangGraphRunner(SimpleNamespace(), str(tmp_path)))
    handle = RunHandle(runtime_type="langgraph", run_id="run", session_id="session")
    operations = [create(), components([{"id": "root", "component": "Text", "text": "Input"}])]
    first = _a2ui_surface_event(adapter, handle, {"tool_output": {"a2ui_operations": operations}, "call_id": "generate"})
    assert len(first) == 2 and "interaction_id" not in first[-1].source.metadata
    before = list(handle.native_ref["a2ui_presentation"]["history"])
    event = NativeInteractionRequested(schema_version=2, event_id="event-wait", seq=3,
        timestamp=1, run_id="run", scope_id="scope", parent_scope_id="parent", source=SourceRef(framework="langgraph"),
        interaction_id="wait", interaction_kind="structured_input",
        request=StructuredInputRequest(prompt="Input", schema={"type": "string"}))
    event._presentation = {"a2ui": {"messages": operations}}
    different = event.model_copy(deep=True)
    different._presentation["a2ui"]["messages"][-1]["updateComponents"]["components"][0]["text"] = "Changed"
    assert _a2ui_surface_event(adapter, handle, different) == ()
    assert handle.native_ref["a2ui_presentation"]["history"] == before
    promoted = _a2ui_surface_event(adapter, handle, event)
    assert len(promoted) == 2
    assert promoted[-1].source.metadata["interaction_id"] == "wait"
    assert promoted[-1].source.metadata["presentation_snapshot"] is True
    from ksadk.agui._agent_helpers import a2ui_operations
    assert promoted[-1].source.metadata["operation_batch"] is True
    assert a2ui_operations(promoted[-1], "card") == []
    assert all(item.parent_scope_id == "parent" for item in promoted)
    assert handle.native_ref["a2ui_presentation"]["history"] == before
    assert _a2ui_surface_event(adapter, handle, event) == ()

    # Another wait cannot take an already-bound surface's active generation.
    other = event.model_copy()
    other.interaction_id = "another-wait"
    assert _a2ui_surface_event(adapter, handle, other) == ()
    assert handle.native_ref["a2ui_presentation"]["interactions"] == {"card": "wait"}


async def test_real_mcp_shaped_tool_result_streams_and_replays_without_losing_business_output(
    tmp_path,
):
    calls = []

    @tool
    def query_resource(resource: str) -> dict:
        """Query a synthetic resource and return a declared A2UI MCP content block."""
        calls.append(resource)
        operations = [create(), components([{
            "id": "root", "component": "Text", "text": "Resource found",
        }])]
        return {"content": [{"type": "text", "text": json.dumps({"a2ui_operations": operations})}]}

    model = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[{
        "name": "query_resource", "args": {"resource": "demo"}, "id": "query",
    }]), AIMessage(content="Query complete")])
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = create_agent(model, tools=[query_resource], checkpointer=InMemorySaver())
    adapter = LangGraphRuntimeAdapter(runner)
    try:
        handle = await adapter.start(StartRequest(input="query", user_id="u", session_id="s"))
        events = [event async for event in adapter.stream(handle)]
        cards = [event for event in events if event.source.protocol == "a2ui"]
        assert len(cards) == 2 and cards[0].event_type == "item.started"
        assert cards[1].event_type == "item.completed" and calls == ["demo"]
        assert events.index(cards[0]) < len(events) - 1
        assert events[-1].event_type == "run.completed"
        live = StreamReducer()
        replay = StreamReducer()
        # Producer seq values are placeholders; the real Session log assigns
        # the sole durable publication cursor before consumers reduce them.
        service = LocalSessionService(project_dir=str(tmp_path))
        await service.create_session("agent", "u", "s")
        store = RuntimeEventStore(service)
        try:
            for event in events:
                live.apply(await store.append_one("s", event))
            for event in await store.list("s", run_id=handle.run_id):
                replay.apply(event)
        finally:
            await service.aclose()
        assert live.snapshot() == replay.snapshot()
        assert any("Query complete" in event.model_dump_json() for event in events)
    finally:
        await runner.close()


async def test_public_generation_tool_and_native_wait_share_one_bound_surface(tmp_path):
    from typing import TypedDict
    from langchain_core.messages import HumanMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode
    from ksadk.a2ui.catalog import catalog_manifest
    from ksadk.compat.copilotkit_a2ui import build_ksadk_a2ui_tool
    from ksadk.interaction.langgraph import request_input
    from ksadk.interaction.native_events import NativeInteractionRequested

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return SimpleNamespace(tool_calls=[{"name": "render_a2ui", "args": {
                "surfaceId": "generated-input", "data": {"answer": ""}, "components": [
                    {"id": "root", "component": "TextField", "label": "Value", "value": {"path": "/answer"}},
                ],
            }}])

    class State(TypedDict, total=False):
        messages: list

    generator = build_ksadk_a2ui_tool({"model": Model()})

    def prepare(_):
        return {"messages": [HumanMessage("Create an input"), AIMessage(content="", tool_calls=[{
            "name": generator.name, "args": {"intent": "create"}, "id": "generate-input",
        }])]}

    def wait(state):
        operations = json.loads(state["messages"][-1].content)["a2ui_operations"]
        request_input("Value", {"type": "string"}, presentation={"a2ui": {
            "wire_version": "0.9.1", "catalog_digest": catalog_manifest()["catalog_digest"],
            "messages": operations,
        }})
        return {}

    graph = StateGraph(State)
    for name, node in [("prepare", prepare), ("generate", ToolNode([generator])), ("wait", wait)]:
        graph.add_node(name, node)
    for source, target in [(START, "prepare"), ("prepare", "generate"), ("generate", "wait"), ("wait", END)]:
        graph.add_edge(source, target)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = graph.compile(checkpointer=InMemorySaver())
    adapter = LangGraphRuntimeAdapter(runner)
    try:
        handle = await adapter.start(StartRequest(input="collect", user_id="u", session_id="s"))
        events = [event async for event in adapter.stream(handle)]
        request = next(event for event in events if isinstance(event, NativeInteractionRequested))
        surfaces = [event for event in events if event.source.protocol == "a2ui"]
        assert len(surfaces) == 4
        assert all(event.source.metadata["surface_id"] == "generated-input" for event in surfaces)
        assert surfaces[-1].source.metadata["interaction_id"] == request.interaction_id
        assert len(handle.native_ref["a2ui_presentation"]["history"]) == 3
    finally:
        await runner.close()
