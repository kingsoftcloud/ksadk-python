"""The official AG-UI seam must not audit native answers before consumption."""

from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.events.store import RuntimeEventStore
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService
from tests.agui.test_runtime_adapter import _input
from tests.interaction.test_langgraph_upgrade_semantics import EvenAnswer
from tests.interaction.test_native_confirmation_pipeline import State


async def generic_input_agent(tmp_path, calls):
    from ksadk.interaction.langgraph import request_input

    def wait(_state):
        value = request_input("Provide a JSON value", {})
        calls.append(value)
        return {"answer": "accepted"}

    builder = StateGraph(State)
    builder.add_node("wait", wait)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    store = RuntimeEventStore(service)
    adapter = LangGraphRuntimeAdapter(runner)
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: adapter)
    agent = KsadkAGUIAgent(
        name="agent",
        executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        event_store_factory=lambda: store,
    )
    return runner, agent, store


@pytest.mark.parametrize("answer", [False, 0, "", [], {"note": None}])
async def test_generic_json_answer_preserves_presence_and_survives_audit_storage(tmp_path, answer):
    calls = []
    runner, agent, store = await generic_input_agent(tmp_path, calls)
    try:
        first = [event async for event in agent.run(_input())]
        public_id = first[-1].outcome.interrupts[0].id
        missing = [
            event
            async for event in agent.run(
                _input(
                    run_id="missing",
                    resume=[
                        {
                            "interruptId": public_id,
                            "status": "resolved",
                        }
                    ],
                )
            )
        ]
        assert missing[-1].code == "INPUT_RESPONSE_INVALID"
        assert not calls
        accepted = [
            event
            async for event in agent.run(
                _input(
                    run_id="accepted",
                    resume=[
                        {
                            "interruptId": public_id,
                            "status": "resolved",
                            "payload": answer,
                        }
                    ],
                )
            )
        ]
        assert accepted[-1].outcome.type == "success"
        assert calls == [answer]
        resolved = [
            event
            for event in await store.list("thread-1")
            if event.event_type == "interaction.resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].response.data == answer
    finally:
        await runner.close()


async def test_root_null_uses_canonical_control_instead_of_a_permissive_agui_model(tmp_path):
    calls = []
    runner, agent, store = await generic_input_agent(tmp_path, calls)
    try:
        first = [event async for event in agent.run(_input())]
        public_id = first[-1].outcome.interrupts[0].id
        null = [
            event
            async for event in agent.run(
                _input(
                    run_id="null",
                    resume=[
                        {
                            "interruptId": public_id,
                            "status": "resolved",
                            "payload": None,
                        }
                    ],
                )
            )
        ]
        assert null[-1].code == "INPUT_TRANSPORT_UNSUPPORTED"
        assert not calls
        assert not any(
            event.event_type == "interaction.resolved" for event in await store.list("thread-1")
        )
    finally:
        await runner.close()


@pytest.mark.parametrize("response_text", [False, True])
async def test_typed_native_rejection_is_correctable_and_only_accepted_answer_is_audited(
    tmp_path, response_text
):
    tools = []

    def wait(_state):
        answer = interrupt("Even amount", response_schema=EvenAnswer)
        tools.append(answer.amount)
        return {"answer": f"accepted {answer.amount}"}

    builder = StateGraph(State)
    builder.add_node("wait", wait)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    store = RuntimeEventStore(service)
    adapter = LangGraphRuntimeAdapter(runner)
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: adapter)
    agent = KsadkAGUIAgent(
        name="agent",
        executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        event_store_factory=lambda: store,
    )
    try:
        events = [event async for event in agent.run(_input())]
        pending = events[-1].outcome.interrupts[0]
        assert pending.reason == "structured_input"
        assert pending.response_schema == EvenAnswer.model_json_schema()

        async def resume(value, run_id):
            return [
                event
                async for event in agent.run(
                    _input(
                        run_id=run_id,
                        resume=[
                            {
                                "interruptId": pending.id,
                                "status": "resolved",
                                "payload": {"amount": str(value) if response_text else value},
                            }
                        ],
                    )
                )
            ]

        rejected = await resume(3, "rejected")
        assert rejected[-1].outcome.type == "interrupt"
        assert rejected[-1].outcome.interrupts[0].id == pending.id
        assert not tools
        assert not any(
            event.event_type == "interaction.resolved" for event in await store.list("thread-1")
        )
        accepted = await resume(0, "accepted")
        assert accepted[-1].outcome.type == "success"
        assert tools == [0]
        resolved = [
            event
            for event in await store.list("thread-1")
            if event.event_type == "interaction.resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0].interaction_kind == "structured_input"
        assert resolved[0].response.data == {"amount": 0}
        duplicate = await resume(0, "duplicate")
        assert duplicate[-1].result == {"status": "already_resumed"}
        assert tools == [0]
    finally:
        await runner.close()


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_native_cancel_never_becomes_an_answer_or_executes_the_business_tool(
    tmp_path, cleanup_failure
):
    tools = []

    def wait(_state):
        answer = interrupt("Even amount", response_schema=EvenAnswer)
        tools.append(answer.amount)
        return {"answer": "finished"}

    builder = StateGraph(State)
    builder.add_node("wait", wait)
    builder.add_edge(START, "wait")
    builder.add_edge("wait", END)
    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = builder.compile(checkpointer=InMemorySaver())
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    store = RuntimeEventStore(service)
    adapter = LangGraphRuntimeAdapter(runner)
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: adapter)
    agent = KsadkAGUIAgent(
        name="agent",
        executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        event_store_factory=lambda: store,
    )
    if cleanup_failure:
        original_close = agent._shared.executor.close

        async def fail_cleanup(handle):
            await original_close(handle)
            raise RuntimeError("synthetic cleanup failure")

        agent._shared.executor.close = fail_cleanup
    try:
        first = [event async for event in agent.run(_input())]
        public_id = first[-1].outcome.interrupts[0].id
        cancelled = [
            event
            async for event in agent.run(
                _input(
                    run_id="cancel",
                    resume=[
                        {
                            "interruptId": public_id,
                            "status": "cancelled",
                        }
                    ],
                )
            )
        ]
        assert cancelled[-1].result == {"status": "cancelled"}
        assert cancelled[-1].outcome.type == "cancelled"
        assert not tools
        events = await store.list("thread-1")
        assert len([event for event in events if event.event_type == "run.canceled"]) == 1
        root = next(event for event in events if event.event_type == "run.started")
        cancelled = next(event for event in events if event.event_type == "run.canceled")
        assert cancelled.scope_id == root.scope_id
        assert not any(event.event_type == "interaction.resolved" for event in events)
        assert not agent._shared.threads
        late = [
            event
            async for event in agent.run(
                _input(
                    run_id="late",
                    resume=[
                        {
                            "interruptId": public_id,
                            "status": "resolved",
                            "payload": {"amount": 2},
                        }
                    ],
                )
            )
        ]
        assert late[-1].type.value == "RUN_ERROR"
        assert not tools
    finally:
        await runner.close()
