"""Existing ToolGateway approvals must survive the managed canonical path."""
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from ksadk.a2a.invocation_policy import A2AInvocationPolicy
from ksadk.events.canonical import InteractionRequested, ItemCompleted
from ksadk.events.run_authority import bind_run_event_authority
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from tests.a2a.test_langgraph_invocation_context import setup_runtime
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


def make_runner(graph):
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    runner._agent = graph
    runner._managed_a2a_bootstrap = SimpleNamespace(
        outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id="caller"),
    )
    return runner


@pytest.mark.asyncio
async def test_managed_canonical_gateway_result_requests_approval(monkeypatch):
    @tool
    async def write_file(path: str) -> str:
        """Request permission to write a file."""
        return json.dumps({"ok": False, "type": "approval_required", "approval_request": {
            "id": "approval-write", "tool_name": "write_file", "risk_level": "medium",
            "side_effects": ["workspace_write"],
        }})

    model = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[{
        "name": "write_file", "id": "native-write", "args": {"path": "hello.md"},
    }]), AIMessage(content="waiting")])
    runner = make_runner(create_react_agent(model, [write_file]))
    authority, _, _, _ = await setup_runtime()
    monkeypatch.setattr(authority, "managed_invocation_policy", lambda: A2AInvocationPolicy())
    adapter = LangGraphRuntimeAdapter(runner)
    handle = RunHandle(run_id="public-run", session_id="s", runtime_type="langgraph", native_ref={})
    mapped = []
    original = []
    with bind_run_event_authority(authority):
        async for event in runner.stream_canonical_events({"run_id": "public-run", "session_id": "s", "input": "write"}):
            original.append(event)
            mapped.extend(adapter._chunk_to_event(handle, None, event))
    approvals = [e for e in mapped if isinstance(e, InteractionRequested)]
    assert len(approvals) == 1, [e.model_dump(mode="json") for e in mapped]
    assert approvals[0].request.call_id == "native-write"
    assert handle.native_ref["pending_approval_ids"] == ["approval-write"]
    assert approvals[0].request.detail["arguments"] == {"path": "hello.md"}
    # Repeated values snapshots must not issue another approval.
    assert not any(isinstance(mapped, InteractionRequested)
                   for event in original for mapped in adapter._chunk_to_event(handle, None, event))
    # A restored initial state may already contain this old tool result.
    # Even a later call with the same ID must not adopt that historical result.
    restored = RunHandle(run_id="public-run", session_id="s", runtime_type="langgraph", native_ref={})
    snapshot = next(e for e in reversed(original) if isinstance(e, ItemCompleted)
                    and e.source.metadata.get("channel") == "values")
    adapter._chunk_to_event(restored, None, snapshot)
    assert not any(isinstance(mapped, InteractionRequested)
                   for event in original for mapped in adapter._chunk_to_event(restored, None, event))


@pytest.mark.asyncio
async def test_canonical_gateway_resume_runs_fresh_semantic_turn(monkeypatch):
    inputs = []
    def answer(state):
        inputs.append(state)
        return {"messages": [AIMessage(content="operation completed")]}
    graph = StateGraph(MessagesState)
    graph.add_node("answer", answer)
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    runner = make_runner(graph.compile(checkpointer=InMemorySaver()))
    authority, _, _, _ = await setup_runtime()
    monkeypatch.setattr(authority, "managed_invocation_policy", lambda: A2AInvocationPolicy())
    with bind_run_event_authority(authority):
        for payload in (
            {"input": "write"},
            {"resume": True, "input": {"type": "function_call_output", "_ksadk_gateway_approval_resume": True}},
        ):
            async for _ in runner.stream_canonical_events({"run_id": "public-run", "session_id": "s", **payload}):
                pass
    assert len(inputs) == 2
    assert inputs[-1]["messages"][-1].content == runner._gateway_approval_follow_up_input()


@pytest.mark.asyncio
async def test_managed_takeover_rejects_checkpoint_drift(monkeypatch):
    graph = StateGraph(MessagesState)
    graph.add_node('answer', lambda state: {'messages': [AIMessage(content='done')]})
    graph.add_edge(START, 'answer')
    graph.add_edge('answer', END)
    runner = make_runner(graph.compile(checkpointer=InMemorySaver()))
    authority, _, _, _ = await setup_runtime()
    monkeypatch.setattr(authority, 'managed_invocation_policy', lambda: A2AInvocationPolicy())
    with bind_run_event_authority(authority):
        async for _ in runner.stream_canonical_events({'run_id': 'public-run', 'session_id': 's', 'input': 'first'}):
            pass
        config = await runner._get_session_config('s')
        previous = await runner._agent.aget_state(config)
        await runner._agent.aupdate_state(config, {'messages': [AIMessage(content='later')]})
        with pytest.raises(ValueError, match='A2A_NATIVE_TAKEOVER_CHECKPOINT_DRIFT'):
            async for _ in runner.stream_canonical_events({
                'run_id': 'public-run', 'session_id': 's', 'input': None,
                'checkpoint_resume': True, '_ksadk_managed_takeover': True,
                'framework_ref': {'langgraph': previous.config['configurable']},
            }):
                pass
