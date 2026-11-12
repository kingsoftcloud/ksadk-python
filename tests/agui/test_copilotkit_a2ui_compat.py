from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ksadk.compat import copilotkit_a2ui


def _custom_catalog():
    from ksadk.a2ui.catalog import catalog_document

    document = catalog_document()
    document["$id"] = document["catalogId"] = "https://example.test/catalog"
    return document


def _request(*, inject: bool):
    return SimpleNamespace(
        model=object(),
        tools=[],
        state={
            "ag-ui": {
                "inject_a2ui_tool": inject,
                "a2ui_schema": '{"catalogId":"https://example.test/catalog"}',
            }
        },
    )


def test_builds_ksadk_a2ui_tool_with_host_catalog_contract(monkeypatch):
    captured = {}
    tool = SimpleNamespace(name="generate_a2ui")

    def factory(params):
        captured.update(params)
        return tool

    monkeypatch.setattr(copilotkit_a2ui, "build_ksadk_a2ui_tool", factory)
    middleware = copilotkit_a2ui.KsadkCopilotKitMiddleware()
    request = _request(inject=True)

    assert middleware._maybe_build_a2ui_tool(request) is tool
    assert captured["model"] is request.model
    assert captured["default_catalog_id"] == "https://example.test/catalog"


def test_does_not_create_a2ui_tool_without_host_opt_in(monkeypatch):
    monkeypatch.setattr(
        copilotkit_a2ui,
        "build_ksadk_a2ui_tool",
        lambda _params: (_ for _ in ()).throw(AssertionError("factory should not run")),
    )
    middleware = copilotkit_a2ui.KsadkCopilotKitMiddleware()

    assert middleware._maybe_build_a2ui_tool(_request(inject=False)) is None


def test_uses_compact_hosted_ui_guidelines_by_default(monkeypatch):
    captured = {}
    original = copilotkit_a2ui._resolve_a2ui_tool_params

    def resolve(params):
        captured.update(params)
        return original(params)

    monkeypatch.setattr(copilotkit_a2ui, "_resolve_a2ui_tool_params", resolve)

    copilotkit_a2ui.build_ksadk_a2ui_tool({"model": object()})

    assert captured["guidelines"] is copilotkit_a2ui._KSADK_HOSTED_UI_GUIDELINES


@pytest.mark.asyncio
async def test_a2ui_generation_runs_async_model_on_parent_event_loop():
    class Model:
        loop = None

        def bind_tools(self, _tools, *, tool_choice):
            assert tool_choice == "render_a2ui"
            return self

        async def ainvoke(self, _messages):
            self.loop = asyncio.get_running_loop()
            return SimpleNamespace(
                tool_calls=[
                    {
                        "name": "render_a2ui",
                        "args": {
                            "surfaceId": "component-status",
                            "components": [
                                {
                                    "id": "root",
                                    "component": "Text",
                                    "text": "ready",
                                }
                            ],
                            "data": {},
                        },
                    }
                ]
            )

    model = Model()
    current_loop = asyncio.get_running_loop()
    envelope = await copilotkit_a2ui._generate_a2ui_envelope(
        config={
            "model": model,
            "guidelines": None,
            "default_surface_id": "dynamic-surface",
            "default_catalog_id": "https://example.test/catalog",
            "catalog": _custom_catalog(),
            "recovery": None,
        },
        state={"ag-ui": {}},
        messages=[],
        intent="create",
        target_surface_id=None,
        changes=None,
    )

    assert model.loop is current_loop
    operations = json.loads(envelope)["a2ui_operations"]
    assert operations[0]["createSurface"] == {
        "surfaceId": "component-status",
        "catalogId": "https://example.test/catalog",
    }


@pytest.mark.asyncio
async def test_a2ui_generation_returns_error_after_bounded_timeout(monkeypatch):
    class Model:
        def bind_tools(self, _tools, *, tool_choice):
            return self

        async def ainvoke(self, _messages):
            await asyncio.Event().wait()

    monkeypatch.setattr(copilotkit_a2ui, "_generation_timeout_seconds", lambda: 0.01)
    envelope = await copilotkit_a2ui._generate_a2ui_envelope(
        config={
            "model": Model(),
            "guidelines": None,
            "default_surface_id": "dynamic-surface",
            "default_catalog_id": "https://example.test/catalog",
            "catalog": _custom_catalog(),
            "recovery": None,
        },
        state={"ag-ui": {}},
        messages=[],
        intent="create",
        target_surface_id=None,
        changes=None,
    )

    assert json.loads(envelope)["error"].startswith("A2UI generation exceeded")


@pytest.mark.asyncio
async def test_native_parent_can_continue_after_internal_generation_timeout():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk
    from langgraph.graph import END, START, MessagesState, StateGraph
    from ksadk.events.adapters.langgraph import LangGraphAdapterContext, LangGraphEventAdapter
    from ksadk.events.content import TextContent

    class SlowModel(FakeMessagesListChatModel):
        async def _astream(self, messages, **kwargs):
            yield ChatGenerationChunk(message=AIMessageChunk(content="private draft"))
            await asyncio.Event().wait()

    model = SlowModel(responses=[AIMessage(content="unused")])

    async def generate(_state):
        with pytest.raises(TimeoutError):
            await copilotkit_a2ui._invoke_a2ui_subagent(
                model, prompt="design a card", messages=[], timeout_seconds=0.01,
            )
        return {"messages": [AIMessage(content="fallback ready")]}

    builder = StateGraph(MessagesState)
    builder.add_node("generate", generate)
    builder.add_edge(START, "generate")
    builder.add_edge("generate", END)
    run = await builder.compile().astream_events({"messages": []}, version="v3")
    raw = [event async for event in run]
    assert not [event for event in raw if event.get("method") == "messages"
                and event["params"]["data"][1].get("ksadk_internal_operation") == "a2ui_generation"]
    adapter = LangGraphEventAdapter()
    context = LangGraphAdapterContext(run_id="timeout", graph_run_id="timeout")
    rows = [row for event in raw for row in adapter.map_protocol_event(event, context)]
    text = [part.text for row in rows if row.event_type == "item.completed"
            for part in row.snapshot.parts if isinstance(part, TextContent)]
    assert text == ["fallback ready"]
