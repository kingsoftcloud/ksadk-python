from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.langgraph_runner import LangGraphRunner


class _Chunk:
    content = "first token"
    additional_kwargs: dict = {}


class _ModelLifecycleAgent:
    async def astream_events(self, state, version="v2", config=None):
        yield {
            "event": "on_chat_model_start",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"input": state},
        }
        yield {
            "event": "on_chat_model_stream",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"chunk": _Chunk()},
        }
        yield {
            "event": "on_chat_model_end",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"output": SimpleNamespace(content="first token")},
        }

    def get_state(self, config):
        return SimpleNamespace(config=None)


@pytest.mark.asyncio
async def test_langgraph_stream_exposes_real_model_boundaries() -> None:
    detection = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(detection, ".")
    runner._agent = _ModelLifecycleAgent()

    chunks = [chunk async for chunk in runner.stream({"input": "hello", "session_id": "s"})]
    boundary_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("type")
        in {
            "step_start",
            "model_call_begin",
            "model_call_first_token",
            "model_call_end",
            "step_end",
        }
    ]

    assert [chunk["type"] for chunk in boundary_chunks] == [
        "step_start",
        "model_call_begin",
        "model_call_first_token",
        "model_call_end",
        "step_end",
    ]
    assert {chunk["step_id"] for chunk in boundary_chunks} == {"step_model-1"}
    assert boundary_chunks[1]["model"] == "demo-model"
    assert boundary_chunks[2]["ttft_ms"] >= 0
    assert runner.get_runtime_capabilities()["model_call_boundaries"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_at", ["start", "stream", "unmarked"])
@pytest.mark.parametrize(
    "selection_text", ["AUTO_ENDPOINT_OK", "No matching skill.", "<think>private"]
)
async def test_internal_model_text_is_hidden_but_usage_and_lifecycle_remain(
    mark_at, selection_text
):
    class Agent(_ModelLifecycleAgent):
        async def astream_events(self, state, **kwargs):
            for run_id, content in [("selector", selection_text), ("answer", "AUTO_ENDPOINT_OK")]:
                for phase in ("start", "stream", "end"):
                    event = {
                        "event": f"on_chat_model_{phase}",
                        "run_id": run_id,
                        "name": "test-model",
                        "data": {},
                    }
                    if run_id == "selector" and phase == mark_at:
                        event["metadata"] = {"ksadk_output_visibility": "internal"}
                    message = SimpleNamespace(
                        content=content,
                        additional_kwargs={"reasoning_content": "private reasoning"}
                        if run_id == "selector"
                        else {},
                        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                    )
                    if phase == "stream":
                        event["data"] = {"chunk": message}
                    elif phase == "end":
                        event["data"] = {"output": message}
                    yield event
            yield {"event": "on_chain_end", "data": {"output": {"answer": "AUTO_ENDPOINT_OK"}}}

    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    runner._agent = Agent()
    events = [event async for event in runner.stream({"input": "test"})]
    final = next(e for e in events if e["type"] == "final")
    if mark_at != "unmarked":
        assert [e["delta"] for e in events if e["type"] == "text"] == ["AUTO_ENDPOINT_OK"]
        assert not any(e["type"] == "thinking" for e in events)
        assert final["output"] == "AUTO_ENDPOINT_OK"
    else:
        assert any(e["type"] == "thinking" for e in events)
        if "<think>" not in selection_text:
            assert final["output"] == selection_text + "AUTO_ENDPOINT_OK"
    assert final["usage"]["total_tokens"] == 10
    for kind in ("model_call_begin", "model_call_first_token", "model_call_end"):
        assert {e["model_call_id"] for e in events if e["type"] == kind} == {"selector", "answer"}


@pytest.mark.asyncio
async def test_real_graph_internal_metadata_does_not_hide_answer_or_suffix():
    from typing import TypedDict

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        answer: str

    model = FakeListChatModel(responses=['{"ok":true}', '{"ok":true}'])

    async def select(state, config):
        internal_config = {
            **config,
            "metadata": {
                **(config.get("metadata") or {}),
                "ksadk_output_visibility": "internal",
            },
        }
        await model.ainvoke("select", internal_config)
        return {}

    async def answer(state, config):
        response = await model.ainvoke("answer", config)
        return {"answer": response.content}

    def finalize(state):
        get_stream_writer()({"type": "text", "delta": "\nRESULT_SUFFIX"})
        return {"answer": state["answer"] + "\nRESULT_SUFFIX"}

    graph = StateGraph(State)
    graph.add_node("select", select)
    graph.add_node("answer", answer)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "select")
    graph.add_edge("select", "answer")
    graph.add_edge("answer", "finalize")
    graph.add_edge("finalize", END)
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="agent"), ".")
    runner._agent = graph.compile()
    events = [event async for event in runner.stream({"input": "test"})]
    expected = '{"ok":true}\nRESULT_SUFFIX'
    assert "".join(e["delta"] for e in events if e["type"] == "text") == expected
    assert next(e["output"] for e in events if e["type"] == "final") == expected
    assert sum(e["type"] == "model_call_begin" for e in events) == 2
