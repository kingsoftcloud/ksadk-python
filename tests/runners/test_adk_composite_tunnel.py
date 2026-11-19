from types import SimpleNamespace

import pytest
from google.adk.agents import SequentialAgent

from ksadk.compat.adk_compat import Agent, App, ResumabilityConfig
from ksadk.runners.adk_runner import ADKRunner
from ksadk.tunnel.registry import TunnelRegistry
from ksadk.tunnel.runtime import TunnelBinding, bind_runner
from ksadk.tunnel.server import TunnelAgentPolicy
from ksadk.tunnel.toolset import TunnelToolset


@pytest.mark.parametrize("composite", [True, False])
async def test_adk_tree_isolation_and_tunnel_injection(tmp_path, monkeypatch, composite):
    def native_tool() -> str:
        return "native"

    leaf = Agent(name="leaf", tools=[native_tool])
    if composite:
        nested = SequentialAgent(name="nested", sub_agents=[leaf])
        root = SequentialAgent(name="root", sub_agents=[nested])
    else:
        root = Agent(name="root", tools=[native_tool], sub_agents=[leaf])
    native_app = App(
        name="inventory", root_agent=root, resumability_config=ResumabilityConfig(is_resumable=True)
    )
    monkeypatch.setattr(
        "ksadk.runners.adk_runner.load_agent_module",
        lambda *_: (root, SimpleNamespace(app=native_app)),
    )
    for method in (
        "_apply_json_patch",
        "_apply_mcp_result_patch",
        "_init_short_term_memory",
        "_init_long_term_memory",
        "_init_knowledge_base",
        "_inject_skill_runtime_tools",
        "_inject_builtin_tools",
        "_inject_mcp_toolsets",
    ):
        monkeypatch.setattr(ADKRunner, method, lambda *_: None)

    detection = SimpleNamespace(entry_point="agent.py", agent_variable="root_agent")
    runner = ADKRunner(detection, str(tmp_path))
    registry = TunnelRegistry()
    bind_runner(runner, TunnelBinding(TunnelAgentPolicy("root", "test", services=None), registry))
    try:
        runner.load_agent()
        cloned = runner._agent
        child = cloned.find_agent("leaf")
        assert native_app.root_agent is root
        assert runner._runner.agent is cloned and runner._resumable
        assert child is not leaf and child.root_agent is cloned
        assert leaf.root_agent is root and leaf.tools == [native_tool]
        if not composite:
            assert child.tools == [native_tool] and root.tools == [native_tool]
        target = child if composite else cloned
        original, tunnel = target.tools
        assert original is native_tool
        assert isinstance(tunnel, TunnelToolset) and tunnel._registry is registry
    finally:
        await runner.close()


@pytest.mark.parametrize(
    ("stream", "parallel", "unknown", "sibling_failed"),
    [
        (False, False, True, False),
        (True, False, False, False),
        (True, True, True, False),
        (True, True, False, False),
        (False, True, False, True),
        (True, True, False, True),
    ],
)
async def test_next_adk_turn_after_tunnel_failure_has_complete_history(
    tmp_path, stream, parallel, unknown, sibling_failed
):
    from unittest.mock import AsyncMock

    from google.adk.models.base_llm import BaseLlm
    from mcp.types import Tool

    from ksadk.compat.adk_compat import InMemorySessionService, LlmResponse, Runner
    from ksadk.compat.adk_compat import genai_types as types
    from ksadk.tunnel.protocol import TunnelError
    from ksadk.tunnel.toolset import TunnelTool

    registry = TunnelRegistry()
    registry.call_tool = AsyncMock(side_effect=TunnelError("busy", indeterminate=unknown))
    remote = TunnelTool(
        agent_id="root",
        service="inventory",
        connector_id="fixture",
        registry=registry,
        descriptor=Tool(name="lookup", inputSchema={"type": "object", "properties": {}}),
    )
    requests = []

    def healthy() -> str:
        return "native result"

    def sibling_tool() -> str:
        if sibling_failed:
            raise TunnelError("busy")
        return "native result"

    class Model(BaseLlm):
        model: str = "fixture"

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request.contents)
            if len(requests) <= 2:
                name = "healthy" if len(requests) == 1 else remote.name
                parts = [types.Part(function_call=types.FunctionCall(id=name, name=name, args={}))]
                if parallel and name == remote.name:
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id="sibling", name="sibling_tool", args={}
                            )
                        )
                    )
            else:
                parts = [types.Part(text="follow-up succeeded")]
            yield LlmResponse(content=types.Content(role="model", parts=parts))

    agent = Agent(name="inventory", model=Model(), tools=[healthy, sibling_tool, remote])
    runner = ADKRunner(SimpleNamespace(name=agent.name), str(tmp_path))
    bind_runner(runner, TunnelBinding(TunnelAgentPolicy("root", "test", services=None), registry))
    runner._agent = agent
    runner._session_service = InMemorySessionService()
    runner._runner = Runner(
        app=App(name="inventory_app", root_agent=agent), session_service=runner._session_service
    )
    try:
        if stream:
            events = [
                e
                async for e in runner.stream_canonical_events({"input": "first", "run_id": "first"})
            ]
            assert events[-1].event_type == "run.completed"
            assert [event.seq for event in events] == list(range(1, len(events) + 1))
            public_metadata = events[-1].source.metadata
        else:
            first = await runner.invoke({"input": "first"})
            public_metadata = first["metadata"]
        assert (await runner.invoke({"input": "second"}))["output"] == "follow-up succeeded"
        assert len(requests) == 3
        parts = [part for content in requests[-1] for part in content.parts]
        for index, content in enumerate(requests[-1]):
            calls = [p.function_call for p in content.parts if p.function_call]
            if calls:
                # Each model call batch must be followed immediately by one
                # matching result per id. Parallel responses can finish in
                # a different order from dispatch.
                results = [p.function_response for p in requests[-1][index + 1].parts]
                assert sorted((r.id, r.name) for r in results) == sorted(
                    (c.id, c.name) for c in calls
                )
        session = await runner._session_service.get_session(
            app_name="inventory_app", user_id="ksadk_user", session_id=runner._default_session_id
        )
        native_responses = [r for event in session.events for r in event.get_function_responses()]
        by_id = {r.id: r.response for r in native_responses}
        assert len(by_id) == len(native_responses) == 2 + int(parallel)
        assert by_id["healthy"] == {"result": "native result"}
        if parallel:
            # Newer ADK persists finished sibling results even when the
            # overall batch fails; earlier ADK may only record the calls.
            sibling = by_id["sibling"]
            if "error" in sibling:
                assert sibling["error"] == "tool_result_unavailable" and sibling["indeterminate"]
            else:
                assert sibling == {"result": "native result"}
            if sibling_failed:
                assert sibling["error"] == "tool_result_unavailable"
        expected_unknown = unknown or (parallel and "error" in by_id["sibling"])
        assert by_id[remote.name]["indeterminate"] is expected_unknown
        notification = "结果未知" if expected_unknown else "工具调用失败"
        assert notification in parts[-2].text and parts[-1].text == "second"
        assert public_metadata["tunnel_failure"]["outcome"] == (
            "unknown" if expected_unknown else "failed"
        )
        if stream:
            public_results = {
                part.call_id: part.result
                for event in events
                if getattr(event, "event_type", "") == "item.completed"
                and event.item_kind == "tool_result"
                for part in event.snapshot.parts
            }
            assert public_results == by_id
        else:
            assert notification in first["output"]
        registry.call_tool.assert_awaited_once()
    finally:
        await runner.close()
