import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent

from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.langgraph_managed import handoff_tool
from ksadk.events.run_authority import bind_run_event_authority
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.toolsets.a2a import get_a2a_tools
from tests.a2a.test_invocation_service_r85 import request
from tests.a2a.test_langgraph_invocation_context import setup_runtime
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [False, True])
async def test_real_react_tool_call_binds_native_identity_and_sends_once(monkeypatch, fixed):
    authority, service, wire, _ = await setup_runtime()
    spec = request().spec
    monkeypatch.setenv("KSADK_A2A_SPACE_ID", spec.space_id)
    policy = A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow"))
    monkeypatch.setattr(authority, "managed_invocation_policy", lambda: policy)
    async def discover():
        return [SimpleNamespace(agent_id=spec.target_agent_id, version_id=spec.expected_version_id,
                                agent_card=SimpleNamespace(name="expert", description="test"))]
    name = "expert" if fixed else "call_a2a_agent"
    args = {"message": "hello"} if fixed else {"agent": "expert", "message": "hello"}
    model = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": "native-call-1"}
    ]), AIMessage(content="finished")])
    graph = create_react_agent(model, [handoff_tool(spec)] if fixed else get_a2a_tools())
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    runner._agent = graph
    runner._managed_a2a_bootstrap = SimpleNamespace(
        outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_service_for_space=lambda *_, **__: service,
        client_for_space=lambda _: SimpleNamespace(discover=discover),
    )
    async def collect():
        events = []
        with bind_run_event_authority(authority):
            async for event in runner.stream_canonical_events(
                {"run_id": "public-run", "session_id": "s", "input": "hello"}
            ):
                await authority.persist_one("s", event)
                events.append(event)
        return events
    events = await asyncio.wait_for(collect(), timeout=15)
    diagnostics = [e.model_dump(mode="json") for e in events
                   if e.event_type in {"run.failed", "item.started", "item.completed"}]
    assert events[-1].event_type == "run.completed", json.dumps(diagnostics)
    assert sum(kind == "send" for kind, _ in wire.calls) == 1, json.dumps(diagnostics)
    # The service must retain the actual native tool ID, not a name/text hash.
    assert any(state.handle.call_id == "native-call-1" for state in service._states.values())
    assert any(e.source.framework == "a2a" for e in events)


def test_compiled_graph_exposure_finds_fixed_tool_and_blocks_generic_duplicate():
    from ksadk.a2a.langgraph_tool_runtime import graph_exposure_plan

    spec = request().spec
    graph = create_react_agent(SeqToolModel(msgs=[AIMessage(content="ok")]),
                               [handoff_tool(spec), *get_a2a_tools()])
    plan = graph_exposure_plan(graph)
    assert len(plan.entries) == 1
    assert plan.entries[0].spec == spec
    with pytest.raises(ValueError, match="A2A_EXPOSURE_CONFLICT"):
        plan.assert_generic_target_allowed(spec.target_agent_id)


def test_managed_live_channel_does_not_replace_native_checkpoint_capabilities():
    from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter

    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    adapter = LangGraphRuntimeAdapter(runner)
    native = adapter.capabilities()
    adapter.configure_managed_a2a(SimpleNamespace(outbound_enabled=True))
    managed = adapter.capabilities()
    assert managed.submit_interaction.supported
    assert managed.submit_interaction.reason == "managed_a2a_live_interaction_only"
    for field in ("resume", "checkpoint", "durable_restore", "interaction_mode"):
        assert getattr(managed, field) == getattr(native, field)


@pytest.mark.asyncio
async def test_discovery_transport_errors_do_not_expose_private_details():
    from ksadk.a2a.langgraph_managed import bind_managed_langgraph

    class Runtime:
        async def discover(self, *args):
            raise RuntimeError("https://private-host?credential=do-not-publish")

    tool = get_a2a_tools()[0]
    with bind_managed_langgraph(None, None, None, runtime=Runtime()):
        result = await tool.ainvoke({})
    assert result == {"ok": False, "error_type": "A2A_DISCOVERY_FAILED",
                      "error_message": "Managed A2A discovery failed"}
