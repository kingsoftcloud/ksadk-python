"""The ordinary Runtime App supports opted-in durable input without Kernel env flags."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.agui.config import AGUIConfig
from ksadk.interaction.langgraph import request_input
from ksadk.kernel import bootstrap, ingress
from ksadk.kernel.local_interactions import LocalInteractionRuntime
from ksadk.kernel.state import RunState
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.local_service import LocalSessionService
from tests.agui.test_kernel_native_input import body, until
from tests.agui.test_runtime_adapter import _input
from tests.interaction.test_native_confirmation_pipeline import State


@asynccontextmanager
async def local_app(tmp_path, monkeypatch, before, tools):
    for key in (
        "KSADK_AGENT_KERNEL",
        "AGENT_KERNEL_ENABLED",
        "AGENT_CONTROL_JWKS_URL",
        "KSADK_CHECKPOINT_BACKEND",
        "KSADK_CHECKPOINT_PATH",
        "KSADK_DEPLOYMENT_MODE",
        "KSADK_LANGGRAPH_CHECKPOINT_DSN",
        "KSADK_CHECKPOINT_DSN",
        "KSADK_SESSION_DSN",
        "AGENT_INSTANCE_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "local")
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "1")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    detection = SimpleNamespace(name="agent", type=SimpleNamespace(value="langgraph"))

    def provider(_):
        def initialize(_state):
            before.append(1)
            return {"context": {"private": "internal-context-marker"}}

        def wait(_state):
            value = request_input("Choose a value", {"type": ["integer", "null"]})
            tools.append(value)
            return {"answer": f"accepted {value}"}

        graph = StateGraph(State)
        graph.add_node("initialize", initialize)
        graph.add_node("wait", wait)
        graph.add_edge(START, "initialize")
        graph.add_edge("initialize", "wait")
        graph.add_edge("wait", END)
        runner = LangGraphRunner(detection, str(tmp_path))
        runner._agent = graph.compile()
        return LangGraphRuntimeAdapter(runner)

    service = LocalSessionService(project_dir=str(tmp_path))
    if await service.get_session("thread-1") is None:
        await service.create_session("agent", "user-1", "thread-1")
    registry = RuntimeRegistry()
    registry.register("langgraph", provider)
    executor = RuntimeExecutor(registry)
    launch = RuntimeLaunchContext(
        runtime_type="langgraph", project_dir=tmp_path, detection=detection
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            yield SimpleNamespace(app=app, client=client, service=service, launch=launch)


async def ask(s, *, run_id="run-1", thread_id="thread-1"):
    request = _input(run_id=run_id).model_copy(update={"thread_id": thread_id})
    async with asyncio.timeout(10):
        response = await s.client.post(
            "/agentengine/agui",
            json=request.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        )
    assert response.status_code == 200, response.text
    events = [
        json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    assert events[-1]["type"] == "RUN_FINISHED", events
    assert events[-1]["outcome"]["type"] == "interrupt"
    assert "internal-context-marker" not in response.text
    runtime = s.app.state.runtime.local_interactions.runtime
    return (await runtime.kernel_store.list_pending_interactions("local", thread_id))[0]


async def test_default_app_enables_native_input_lazily_and_preserves_other_routes(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        cold = await s.client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "agent"}
        )
        assert cold.status_code == 200
        assert cold.json()["Data"]["Capabilities"]["InteractionV1"] is False
        assert cold.json()["Data"]["HostedChat"]["Transports"][0]["Capabilities"]["Interrupt"]
        assert not before and not tools and bootstrap.get_agent_kernel_runtime() is None
        record = await ask(s)
        runtime = s.app.state.runtime.local_interactions.runtime
        facts = await runtime.session_events.read("thread-1", 0, 200)
        barriers = [fact for fact in facts if fact.event_type == "control.interaction_wait_ready"]
        assert len(barriers) == 1 and barriers[0].family == "control"
        assert barriers[0].payload["interaction_ids"] == [record.interaction_id]
        assert not any(
            fact.family == "runtime"
            and fact.payload.get("source", {}).get("metadata", {}).get("interaction_wait_ready")
            for fact in facts
        )
        assert runtime.config.local_interactions and not ingress.kernel_route_active()
        warm = await s.client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "agent"}
        )
        assert warm.json()["Data"]["Capabilities"]["InteractionV1"] is True
        blocked = await s.client.post(
            "/v1/responses",
            json={
                "input": "must not start another turn",
                "user": "user-1",
                "conversation": "thread-1",
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert before == [1] and not tools
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, 0)
        )
        assert response.status_code == 202, response.text

        async def completed():
            return (await runtime.kernel_store.load_run(record.run_id)).state is RunState.COMPLETED

        await until(completed)
        assert before == [1] and tools == [0]
    assert bootstrap.get_agent_kernel_runtime() is None and ingress.get_agent_kernel() is None


async def test_cold_submit_after_restart_restores_the_original_native_wait(tmp_path, monkeypatch):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        record = await ask(s)
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        assert s.app.state.runtime.local_interactions.runtime is None
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, None)
        )
        assert response.status_code == 202, response.text
        runtime = s.app.state.runtime.local_interactions.runtime

        async def completed():
            return (await runtime.kernel_store.load_run(record.run_id)).state is RunState.COMPLETED

        await until(completed)
        assert before == [1] and tools == [None]


async def test_duplicate_official_resume_replays_facts_without_repeating_execution(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        record = await ask(s)
        request = _input(
            run_id="same-answer-attempt",
            resume=[
                {
                    "interruptId": record.interaction_id,
                    "status": "resolved",
                    "payload": 0,
                }
            ],
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        for _ in range(2):
            async with asyncio.timeout(8):
                response = await s.client.post("/agentengine/agui", json=request)
            events = [
                json.loads(line[5:])
                for line in response.text.splitlines()
                if line.startswith("data:")
            ]
            assert events[-1]["type"] == "RUN_FINISHED", events
            assert events[-1]["outcome"]["type"] == "success", events
        assert before == [1] and tools == [0]
        runtime = s.app.state.runtime.local_interactions.runtime
        assert len(await runtime.kernel_store.list_messages("local-agent")) == 2


async def test_wire_cancel_and_multiple_sessions_share_app_owned_checkpoint_lifetime(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        first = await ask(s)
        await s.service.create_session("agent", "user-1", "second")
        second = await ask(s, run_id="second-attempt", thread_id="second")
        cancelled = await s.client.post(
            "/agentengine/api/v1/CancelRun",
            json={
                "AgentId": "agent",
                "UserId": "user-1",
                "SessionId": "thread-1",
                "InvocationId": "run-1",
            },
        )
        assert cancelled.status_code == 200 and cancelled.json()["Data"]["Cancelled"] is True
        runtime = s.app.state.runtime.local_interactions.runtime

        async def cancelled_fact():
            return (await runtime.kernel_store.load_run(first.run_id)).state is RunState.CANCELLED

        await until(cancelled_fact)
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(second, 0)
        )
        assert response.status_code == 202

        async def completed():
            return (await runtime.kernel_store.load_run(second.run_id)).state is RunState.COMPLETED

        await until(completed)
        assert before == [1, 1] and tools == [0]


@pytest.mark.parametrize("setting", ["hosted", "deployment", "disabled"])
async def test_local_automatic_bootstrap_never_overrides_explicit_or_hosted_configuration(
    tmp_path,
    monkeypatch,
    setting,
):
    monkeypatch.delenv("KSADK_AGENT_KERNEL", raising=False)
    monkeypatch.delenv("AGENT_KERNEL_ENABLED", raising=False)
    monkeypatch.delenv("KSADK_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted" if setting == "hosted" else "local")
    if setting == "disabled":
        monkeypatch.setenv("AGENT_KERNEL_ENABLED", "0")

    def forbidden():
        pytest.fail("Forbidden automatic adapter creation")

    manager = LocalInteractionRuntime(
        provider=forbidden,
        agent_name="agent",
        session_service=forbidden,
        launch_context=RuntimeLaunchContext(
            runtime_type="langgraph",
            project_dir=tmp_path,
            deployment_mode="ksadk_managed_cloud" if setting == "deployment" else "local",
        ),
    )
    assert await manager.ensure() is None
    await manager.close()
