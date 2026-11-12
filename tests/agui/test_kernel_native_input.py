"""Real Kernel/SQLite/LangGraph integration, including HTTP control admission."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from langgraph.graph import END, START, StateGraph

from ksadk.agui.config import AGUIConfig
from ksadk.agui.routes import add_ksadk_agui_endpoint
from ksadk.interaction.langgraph import request_input
from ksadk.kernel import bootstrap, ingress
from ksadk.kernel.bootstrap import AgentKernelRuntimeConfig, build_agent_kernel_runtime
from ksadk.kernel.state import RunState
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.server.routes import interactions as routes
from ksadk.sessions.local_service import LocalSessionService
from tests.agui.test_runtime_adapter import _input
from tests.interaction.test_native_confirmation_pipeline import State


async def until(check):
    async with asyncio.timeout(8):
        while not await check():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def stack(tmp_path, monkeypatch, before, tools, *, sequential=False, graph_builder=None):
    for key, value in {
        "KSADK_AGENT_KERNEL": "1",
        "AGENT_KERNEL_AUTHORITY_MODE": "local",
        "KSADK_LANGGRAPH_AUTO_CHECKPOINT": "1",
        "KSADK_CHECKPOINT_BACKEND": "local",
        "AGENT_INSTANCE_ID": "local-agent",
    }.items():
        monkeypatch.setenv(key, value)

    def initialize(_):
        before.append(1)
        return {"context": {"private": "must-never-be-chat"}}

    def wait(_):
        answer = request_input("Choose a value", {"type": ["integer", "null"]})
        if sequential:
            answer = request_input("Choose a value", {"type": ["integer", "null"]})
        tools.append(answer)
        return {"answer": f"accepted {answer}"}

    builder = StateGraph(State)
    builder.add_node("initialize", initialize)
    builder.add_node("wait", wait)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "wait")
    builder.add_edge("wait", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = (graph_builder or builder).compile()
    adapter = LangGraphRuntimeAdapter(runner)
    await adapter.preflight()
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    if await service.get_session("thread-1") is None:
        await service.create_session("agent", "user-1", "thread-1")
    launch = RuntimeLaunchContext(
        runtime_type="langgraph", project_dir=tmp_path, config={"tenant_id": "local"}
    )
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: adapter)
    executor = RuntimeExecutor(registry)
    runtime = build_agent_kernel_runtime(
        AgentKernelRuntimeConfig(
            agent_instance_id="local-agent",
            tenant_id="local",
            driver="sqlite",
            dsn=str(tmp_path / "sessions.sqlite"),
            session_service=service,
            adapter_provider=lambda: adapter,
            runtime_executor=executor,
            launch_context=launch,
            start_request_defaults={"agent_id": "agent"},
            poll_interval=0.01,
        )
    )
    ingress.set_agent_kernel(runtime.kernel)
    bootstrap.set_agent_kernel_runtime(runtime)
    await runtime.start()
    app = FastAPI()
    app.include_router(routes.control_router)
    agent = add_ksadk_agui_endpoint(
        app,
        executor,
        launch,
        AGUIConfig(enabled=True, agent_name="agent"),
        session_service_factory=lambda: service,
    )
    monkeypatch.setattr(routes.deps, "resolve_session_service", lambda: service)
    monkeypatch.setattr(routes, "get_runtime_execution", lambda: (executor, launch))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            yield SimpleNamespace(agent=agent, runtime=runtime, client=client, adapter=adapter)
    finally:
        await runtime.close()
        await runner.close()
        bootstrap.clear_agent_kernel_runtime()
        ingress.clear_agent_kernel()


async def first_wait(s):
    async with asyncio.timeout(8):
        events = [event async for event in s.agent.run(_input())]
    assert events[-1].outcome.type == "interrupt", [e.model_dump() for e in events]
    assert "must-never-be-chat" not in json.dumps([e.model_dump() for e in events])
    custom = next(event for event in events if event.type == "CUSTOM")
    assert custom.value["family"] == "interaction"
    assert "native_target" not in str(custom.value) and "checkpoint_id" not in str(custom.value)
    interrupt = events[-1].outcome.interrupts[0]
    assert interrupt.message == "Choose a value"
    assert interrupt.metadata["interaction_v1"] is True
    record = (await s.runtime.kernel_store.list_pending_interactions("local", "thread-1"))[0]
    assert record.interaction_id == interrupt.id
    execution = s.runtime.worker.execution_for(record.run_id)
    await until(lambda: asyncio.sleep(0, result=execution.stream_task.done()))
    return record


async def test_official_root_null_is_rejected_before_admission(tmp_path, monkeypatch):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        record = await first_wait(s)
        events = [
            event
            async for event in s.agent.run(
                _input(
                    run_id="null",
                    resume=[
                        {
                            "interruptId": record.interaction_id,
                            "status": "resolved",
                            "payload": None,
                        }
                    ],
                )
            )
        ]
        assert events[-1].code == "INPUT_TRANSPORT_UNSUPPORTED"
        facts = await s.runtime.session_events.read("thread-1", 0, 1000)
        assert len([e for e in facts if e.event_type == "control.command_accepted"]) == 1
        assert before == [1] and not tools


@pytest.mark.parametrize("answer", [0, "0"])
async def test_official_http_sse_runs_native_kernel_wait_and_original_resume(
    tmp_path, monkeypatch, answer
):
    before, tools = [], []

    def frames(response):
        assert response.status_code == 200, response.text
        assert "text/event-stream" in response.headers["content-type"]
        return [
            json.loads(line[5:].strip())
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]

    async with stack(tmp_path, monkeypatch, before, tools) as s:
        async with asyncio.timeout(8):
            response = await s.client.post(
                "/agentengine/agui",
                json=_input().model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
            )
        first = frames(response)
        assert first[0]["protocolVersion"] == "1.0"
        interrupt = first[-1]["outcome"]["interrupts"][0]
        assert interrupt["responseSchema"] == {"type": ["integer", "null"]}
        assert "must-never-be-chat" not in response.text
        request = _input(
            run_id="http-resume",
            resume=[
                {
                    "interruptId": interrupt["id"],
                    "status": "resolved",
                    "payload": answer,
                }
            ],
        )
        async with asyncio.timeout(8):
            response = await s.client.post(
                "/agentengine/agui",
                json=request.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
            )
        resumed = frames(response)
        assert resumed[-1]["outcome"]["type"] == "success", resumed
        assert before == [1] and tools == [0]
        resolved = [
            event
            for event in resumed
            if event["type"] == "CUSTOM" and event["value"]["event_type"] == "interaction.resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0]["value"]["run_id"] == interrupt["metadata"]["run_id"]


def body(record, response, key="submit"):
    return {
        "AgentId": "agent",
        "UserId": "user-1",
        "SessionId": record.session_id,
        "RunId": record.run_id,
        "InteractionId": record.interaction_id,
        "ExpectedRevision": record.revision,
        "InteractionAction": "submit",
        "Response": response,
        "IdempotencyKey": key,
    }


async def test_parallel_resume_batch_waits_for_both_consumptions(tmp_path, monkeypatch):
    from typing_extensions import TypedDict

    class ParallelState(TypedDict, total=False):
        messages: list
        left: int
        right: int
        answer: str

    before, tools = [], []

    def initialize(_):
        before.append(1)
        return {}

    def branch(name):
        def node(_):
            value = request_input(name, {"type": "integer"})
            tools.append((name, value))
            return {name: value}

        return node

    builder = StateGraph(ParallelState)
    builder.add_node("initialize", initialize)
    builder.add_edge(START, "initialize")
    for name in ("left", "right"):
        builder.add_node(name, branch(name))
        builder.add_edge("initialize", name)
    builder.add_node("finish", lambda state: {"answer": f"{state['left']}+{state['right']}"})
    builder.add_edge(["left", "right"], "finish")
    builder.add_edge("finish", END)
    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:
        async with asyncio.timeout(8):
            initial = [event async for event in s.agent.run(_input())]
        interrupts = initial[-1].outcome.interrupts
        assert len(interrupts) == 2
        async with asyncio.timeout(8):
            events = [
                event
                async for event in s.agent.run(
                    _input(
                        run_id="batch",
                        resume=[
                            {"interruptId": item.id, "status": "resolved", "payload": index}
                            for index, item in enumerate(interrupts)
                        ],
                    )
                )
            ]
        assert events[-1].type == "RUN_FINISHED", [e.model_dump() for e in events]
        assert events[-1].outcome.type == "success", [e.model_dump() for e in events]
        assert len(tools) == 2 and before == [1]
        assert sorted(value for _, value in tools) == [0, 1]
        for item in interrupts:
            assert (await s.runtime.kernel_store.get(item.id)).status == "resolved"


@pytest.mark.parametrize("answer", [0, None])
async def test_real_http_submission_consumes_original_native_wait_and_duplicate_is_harmless(
    tmp_path,
    monkeypatch,
    answer,
):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        record = await first_wait(s)
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, answer)
        )
        assert response.status_code == 202, response.text

        async def completed():
            return (
                await s.runtime.kernel_store.load_run(record.run_id)
            ).state is RunState.COMPLETED

        await until(completed)
        assert before == [1] and tools == [answer]
        duplicate = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, answer)
        )
        assert duplicate.status_code == 200 and duplicate.json()["Data"]["status"] == "duplicate"
        assert tools == [answer]
        events = await s.runtime.session_events.read("thread-1", 0, 1000)
        assert len([event for event in events if event.event_type == "interaction.resolved"]) == 1


async def test_restart_reconstructs_original_native_wait_without_starting_a_new_turn(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        record = await first_wait(s)
    async with stack(tmp_path, monkeypatch, before, tools) as s:

        async def attached():
            return s.runtime.worker.execution_for(record.run_id) is not None

        await until(attached)
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, 0)
        )
        assert response.status_code == 202, response.text

        async def completed():
            return (
                await s.runtime.kernel_store.load_run(record.run_id)
            ).state is RunState.COMPLETED

        await until(completed)
        assert before == [1] and tools == [0]
        assert (await s.runtime.kernel_store.get(record.interaction_id)).status == "resolved"


async def test_new_session_after_restart_cannot_replace_a_restored_native_handle(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        original = await first_wait(s)
    async with stack(tmp_path, monkeypatch, before, tools) as s:

        async def attached():
            return s.runtime.worker.execution_for(original.run_id) is not None

        await until(attached)
        service = s.runtime.config.session_service
        await service.create_session("agent", "user-1", "another-thread")
        async with asyncio.timeout(8):
            events = [
                event
                async for event in s.agent.run(
                    _input(run_id="another-run").model_copy(update={"thread_id": "another-thread"})
                )
            ]
        assert events[-1].outcome.type == "interrupt"
        other = (await s.runtime.kernel_store.list_pending_interactions("local", "another-thread"))[
            0
        ]
        original_handle = s.runtime.worker.execution_for(original.run_id).handle
        other_handle = s.runtime.worker.execution_for(other.run_id).handle
        assert original_handle.run_id != other_handle.run_id
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(original, 0)
        )
        assert response.status_code == 202

        async def completed():
            return (
                await s.runtime.kernel_store.load_run(original.run_id)
            ).state is RunState.COMPLETED

        await until(completed)
        assert tools == [0] and before == [1, 1]
        assert (await s.runtime.kernel_store.get(other.interaction_id)).status == "pending"


async def test_kernel_official_resume_targets_original_run_and_next_occurrence(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools, sequential=True) as s:
        first = await first_wait(s)
        async with asyncio.timeout(8):
            events = [
                event
                async for event in s.agent.run(
                    _input(
                        run_id="resume-1",
                        resume=[
                            {
                                "interruptId": first.interaction_id,
                                "status": "resolved",
                                "payload": 0,
                            }
                        ],
                    )
                )
            ]
        assert events[-1].outcome.type == "interrupt", [e.model_dump() for e in events]
        second = events[-1].outcome.interrupts[0]
        assert second.id != first.interaction_id, [
            (e.event_type, e.payload)
            for e in await s.runtime.session_events.read("thread-1", 0, 1000)
        ]
        assert second.metadata["run_id"] == first.run_id
        assert before == [1] and not tools
        assert (await s.runtime.kernel_store.get(first.interaction_id)).status == "resolved"


async def test_bad_http_input_is_correctable_and_cancel_is_not_an_answer(tmp_path, monkeypatch):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        record = await first_wait(s)
        missing = body(record, 0)
        del missing["Response"]
        assert (
            await s.client.post("/agentengine/api/v1/SubmitInteraction", json=missing)
        ).status_code == 422
        assert (
            await s.client.post(
                "/agentengine/api/v1/SubmitInteraction",
                json={
                    **body(record, 0),
                    "CheckpointId": "client-cannot-select-target",
                },
            )
        ).status_code == 422
        assert (
            await s.client.post(
                "/agentengine/api/v1/SubmitInteraction",
                json={
                    **body(record, 0),
                    "UserId": "different-user",
                },
            )
        ).status_code == 404
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, "wrong")
        )
        assert response.status_code == 202

        async def rejected():
            return any(
                event.event_type == "control.command_rejected"
                for event in await s.runtime.session_events.read("thread-1", 0, 1000)
            )

        await until(rejected)
        assert (await s.runtime.kernel_store.get(record.interaction_id)).status == "pending"
        cancel = {**body(record, None, "cancel"), "InteractionAction": "cancel"}
        assert (
            await s.client.post("/agentengine/api/v1/SubmitInteraction", json=cancel)
        ).status_code == 202

        async def cancelled():
            return (
                await s.runtime.kernel_store.load_run(record.run_id)
            ).state is RunState.CANCELLED

        await until(cancelled)
        assert before == [1] and not tools
        assert (await s.runtime.kernel_store.get(record.interaction_id)).status == "cancelled"


async def test_fast_submission_does_not_attach_initial_agui_stream_to_resumed_activation(
    tmp_path, monkeypatch
):
    before, tools = [], []
    async with stack(tmp_path, monkeypatch, before, tools) as s:
        original = ingress.subscribe_projected
        crossed_barrier = False

        async def subscribe(*args, **kwargs):
            nonlocal crossed_barrier
            async for cursor, envelope in original(*args, **kwargs):
                if envelope.event_type == "control.interaction_wait_ready":
                    record = (await s.runtime.kernel_store.list_pending_interactions(
                        "local", "thread-1"
                    ))[0]
                    response = await s.client.post(
                        "/agentengine/api/v1/SubmitInteraction", json=body(record, 0)
                    )
                    assert response.status_code == 202

                    async def completed():
                        return (await s.runtime.kernel_store.load_run(
                            record.run_id
                        )).state is RunState.COMPLETED

                    await until(completed)
                    crossed_barrier = True
                yield cursor, envelope

        monkeypatch.setattr(ingress, "subscribe_projected", subscribe)
        async with asyncio.timeout(10):
            events = [event async for event in s.agent.run(_input())]
        assert crossed_barrier and before == [1] and tools == [0]
        assert events[-1].outcome.type == "interrupt"
        assert not any(event.type == "TEXT_MESSAGE_CONTENT" for event in events)
        assert not any(
            event.type == "CUSTOM"
            and event.value.get("event_type") == "interaction.resolved"
            for event in events
        )
