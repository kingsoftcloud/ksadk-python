"""File-checkpoint recovery through the public, non-Kernel AG-UI adapter."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.events.canonical import StructuredInputRequest
from ksadk.events.store import RuntimeEventStore
from ksadk.interaction.langgraph import request_input
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService
from tests.agui.test_runtime_adapter import _input
from tests.interaction.test_langgraph_upgrade_semantics import EvenAnswer
from tests.interaction.test_native_confirmation_pipeline import State


@asynccontextmanager
async def opened(tmp_path, builder, store):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.sqlite")) as saver:
        runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
        runner._agent = builder.compile(checkpointer=saver)
        registry = RuntimeRegistry()
        registry.register("langgraph", lambda _: LangGraphRuntimeAdapter(runner))
        executor = RuntimeExecutor(registry)
        agent = KsadkAGUIAgent(
            name="agent", executor=executor,
            launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
            event_store_factory=lambda: store,
        )
        try:
            yield agent
        finally:
            await executor.close_all()


async def event_store():
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    return RuntimeEventStore(service)


async def run(agent, interrupt_id=None, answer=None, run_id="first"):
    kwargs = {"run_id": run_id}
    if interrupt_id is not None:
        kwargs["resume"] = [{"interruptId": interrupt_id, "status": "resolved", "payload": answer}]
    return [event async for event in agent.run(_input(**kwargs))]


def graph(node, before):
    builder = StateGraph(State)

    def initialize(_state):
        before.append(1)
        return {"context": {"private": "unchanged"}}

    builder.add_node("initialize", initialize)
    builder.add_node("wait", node)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "wait")
    builder.add_edge("wait", END)
    return builder


@pytest.mark.parametrize("answer", [False, 0, "", [], {"note": None}])
async def test_cold_json_input_recovers_native_wait_before_resolving(tmp_path, answer):
    calls, before = [], []

    def node(_state):
        calls.append(request_input("Value", {}))
        return {"answer": "done"}

    builder, store = graph(node, before), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        first = await run(agent)
        pending = first[-1].outcome.interrupts[0].id
        interrupted = next(e for e in reversed(await store.list("thread-1"))
                           if e.event_type == "run.interrupted")
        saved_request = next(e for e in await store.list("thread-1")
                             if e.event_type == "interaction.requested")
        assert saved_request.source.metadata["agui_interaction_delivery"] == "native_interrupt"
        assert "native_waits" not in interrupted.source.metadata["runtime_handle"]["native_ref"]
    async with opened(tmp_path, builder, store) as agent:
        completed = await run(agent, pending, answer, "cold-submit")
        assert completed[-1].outcome.type == "success"
        assert calls == [answer] and before == [1]
        resolved = [e for e in await store.list("thread-1") if e.event_type == "interaction.resolved"]
        assert len(resolved) == 1
        assert resolved[0].interaction_kind == "structured_input"
    async with opened(tmp_path, builder, store) as agent:
        duplicate = await run(agent, pending, answer, "cold-duplicate")
        assert duplicate[-1].result == {"status": "already_resumed"}
        assert calls == [answer]


async def test_cold_typed_rejection_keeps_wait_correctable(tmp_path):
    calls, before = [], []

    def node(_state):
        calls.append(interrupt("Even value", response_schema=EvenAnswer).amount)
        return {"answer": "done"}

    builder, store = graph(node, before), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
    async with opened(tmp_path, builder, store) as agent:
        rejected = await run(agent, pending, {"amount": 3}, "bad")
        assert rejected[-1].outcome.type == "interrupt"
        assert rejected[-1].outcome.interrupts[0].id == pending
        assert calls == []
        assert not any(e.event_type == "interaction.resolved" for e in await store.list("thread-1"))
        accepted = await run(agent, pending, {"amount": 0}, "corrected")
        assert accepted[-1].outcome.type == "success"
        assert calls == [0] and before == [1]


async def test_cold_same_task_third_occurrence_preserves_generation(tmp_path):
    calls, before = [], []

    def node(_state):
        values = [request_input("Value", {"type": "integer"}) for _ in range(3)]
        calls.append(values)
        return {"answer": "done"}

    builder, store = graph(node, before), await event_store()
    ids = []
    async with opened(tmp_path, builder, store) as agent:
        events = await run(agent)
        for index in range(2):
            ids.append(events[-1].outcome.interrupts[0].id)
            events = await run(agent, ids[-1], index, f"submit-{index}")
        ids.append(events[-1].outcome.interrupts[0].id)
        assert len(set(ids)) == 3
    async with opened(tmp_path, builder, store) as agent:
        events = await run(agent, ids[-1], 2, "third-cold")
        assert events[-1].outcome.type == "success"
        assert calls == [[0, 1, 2]] and before == [1]


async def test_cold_parallel_partial_wait_does_not_repeat_completed_branch(tmp_path):
    class Parallel(TypedDict, total=False):
        left: int
        right: int

    calls = []
    builder = StateGraph(Parallel)

    def branch(name):
        def node(_state):
            answer = request_input(name, {"type": "integer"})
            calls.append((name, answer))
            return {name: answer}
        return node

    for name in ("left", "right"):
        builder.add_node(name, branch(name))
        builder.add_edge(START, name)
        builder.add_edge(name, END)
    store = await event_store()
    async with opened(tmp_path, builder, store) as agent:
        events = await run(agent)
        waits = {wait.message: wait.id for wait in events[-1].outcome.interrupts}
        events = await run(agent, waits["left"], 0, "left-submit")
        assert [wait.id for wait in events[-1].outcome.interrupts] == [waits["right"]]
        assert calls == [("left", 0)]
    async with opened(tmp_path, builder, store) as agent:
        events = await run(agent, waits["right"], 1, "right-cold")
        assert events[-1].outcome.type == "success"
        assert calls == [("left", 0), ("right", 1)]


async def test_cold_schema_drift_fails_before_audit_or_business_execution(tmp_path, monkeypatch):
    calls = []

    def node(_state):
        calls.append(request_input("Value", {"type": "integer"}))
        return {"answer": "done"}

    builder, store = graph(node, []), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
    async with opened(tmp_path, builder, store) as agent:
        async def altered_read(_session):
            return [event.model_copy(update={"request": StructuredInputRequest(
                schema={"type": "string"}, prompt="Value",
            )}) if event.event_type == "interaction.requested" else event
                for event in await store.list("thread-1")]
        monkeypatch.setattr(agent, "_durable_events", altered_read)
        events = await run(agent, pending, 0, "schema-drift")
        assert events[-1].type.value == "RUN_ERROR"
        assert calls == []
        assert not any(e.event_type == "interaction.resolved" for e in await store.list("thread-1"))


async def test_native_sensitive_answer_is_private_but_replay_still_detects_conflict(tmp_path):
    import json

    marker = "FAKE-REVIEW-SECRET-DO-NOT-USE"
    schema = {"type": "object", "properties": {
        "token": {"type": "string", "writeOnly": True},
        "public": {"type": "integer"},
    }, "required": ["token", "public"]}
    answer = {"token": marker, "public": 0}
    calls = []

    def node(_state):
        calls.append(request_input("Secret", schema))
        return {"answer": "done"}

    builder, store = graph(node, []), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
        assert (await run(agent, pending, answer, "submit"))[-1].outcome.type == "success"
    assert calls == [answer]
    events = await store.list("thread-1")
    resolved = next(event for event in events if event.event_type == "interaction.resolved")
    assert resolved.response.data == {"token": None, "public": 0}
    assert marker not in json.dumps([event.model_dump(mode="json") for event in events])
    async with opened(tmp_path, builder, store) as agent:
        duplicate = await run(agent, pending, answer, "same")
        assert duplicate[-1].result == {"status": "already_resumed"}
        conflict = await run(agent, pending, {**answer, "token": "different"}, "different")
        assert conflict[-1].type.value == "RUN_ERROR"
    assert calls == [answer]


async def test_cold_subgraph_wait_uses_leaf_task_and_keeps_legacy_handle_shape(tmp_path, monkeypatch):
    calls, before = [], []

    def wait(_state):
        calls.append(request_input("Leaf", {"type": "integer"}))
        return {"answer": "done"}

    child = StateGraph(State)
    child.add_node("leaf", wait)
    child.add_edge(START, "leaf")
    child.add_edge("leaf", END)
    builder, store = graph(child.compile(), before), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
    async with opened(tmp_path, builder, store) as agent:
        async def legacy_handle(_session):
            events = await store.list("thread-1")
            result = []
            for event in events:
                if event.event_type == "run.interrupted":
                    source = event.source.model_copy(deep=True)
                    ref = source.metadata["runtime_handle"]["native_ref"]
                    for key in ("thread_id", "checkpoint_ns", "checkpoint_id"):
                        ref.pop(key, None)
                    event = event.model_copy(update={"source": source})
                result.append(event)
            return result
        monkeypatch.setattr(agent, "_durable_events", legacy_handle)
        events = await run(agent, pending, 0, "leaf-cold")
        assert events[-1].outcome.type == "success"
        assert calls == [0] and before == [1]


async def test_cold_cancel_is_not_a_native_answer(tmp_path):
    calls = []

    def wait(_state):
        calls.append(request_input("Value", {"type": "integer"}))
        return {"answer": "done"}

    builder, store = graph(wait, []), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
    async with opened(tmp_path, builder, store) as agent:
        events = [event async for event in agent.run(_input(run_id="cold-cancel", resume=[{
            "interruptId": pending, "status": "cancelled",
        }]))]
        assert events[-1].result == {"status": "cancelled"}
        assert events[-1].outcome.type == "cancelled"
        assert calls == []
        assert not any(e.event_type == "interaction.resolved" for e in await store.list("thread-1"))


async def test_old_plaintext_fingerprint_is_read_without_becoming_a_new_event(tmp_path, monkeypatch):
    import json

    calls = []

    def wait(_state):
        calls.append(request_input("Value", {"type": "integer"}))
        return {"answer": "done"}

    builder, store = graph(wait, []), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
        await run(agent, pending, 0, "submit")
    async with opened(tmp_path, builder, store) as agent:
        async def old_fingerprint(_session):
            events = await store.list("thread-1")
            result = []
            for event in events:
                if event.event_type == "interaction.resolved":
                    source = event.source.model_copy(deep=True)
                    source.metadata["resume_fingerprint"] = json.dumps(["resolved", 0])
                    event = event.model_copy(update={"source": source})
                result.append(event)
            return result
        monkeypatch.setattr(agent, "_durable_events", old_fingerprint)
        duplicate = await run(agent, pending, 0, "legacy-duplicate")
        assert duplicate[-1].result == {"status": "already_resumed"}
        conflict = await run(agent, pending, 1, "legacy-conflict")
        assert conflict[-1].type.value == "RUN_ERROR"
        assert calls == [0]


@pytest.mark.parametrize("prompt, expected_kind", [
    ("Approve operation?", "structured_input"),
    ({"kind": "approval", "message": "Approve operation?"}, "approval"),
])
async def test_cold_native_wait_keeps_its_declared_contract(tmp_path, prompt, expected_kind):
    calls, before = [], []

    def wait(_state):
        calls.append(interrupt(prompt))
        return {"answer": "done"}

    builder, store = graph(wait, before), await event_store()
    async with opened(tmp_path, builder, store) as agent:
        pending = (await run(agent))[-1].outcome.interrupts[0].id
    async with opened(tmp_path, builder, store) as agent:
        events = await run(agent, pending, {"approved": True}, "cold-approval")
        assert events[-1].outcome.type == "success"
        assert calls == [{"approved": True}] and before == [1]
        resolved = [event for event in await store.list("thread-1")
                    if event.event_type == "interaction.resolved"]
        assert len(resolved) == 1 and resolved[0].interaction_kind == expected_kind
        if expected_kind == "approval":
            assert resolved[0].response.decision == "approved"
        else:
            assert resolved[0].response.data == {"approved": True}


@pytest.mark.parametrize("marked", [False, True])
async def test_legacy_semantic_approval_without_native_task_keeps_existing_route(tmp_path, marked):
    from ksadk.events.canonical import (
        ApprovalRequest,
        InteractionRequested,
        ItemCompleted,
        SourceRef,
    )
    from ksadk.events.content import ContentSnapshot, ToolResultContent
    from ksadk.runtime.adapter import RunHandle

    builder = StateGraph(State)
    builder.add_node("legacy", lambda _: {"answer": "approval required"})
    builder.add_edge(START, "legacy")
    builder.add_edge("legacy", END)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "legacy.sqlite")) as saver:
        compiled = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "legacy"}}
        await compiled.ainvoke({}, config)
        snapshot = await compiled.aget_state(config)
        ref = snapshot.config["configurable"]
        runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
        runner._agent = compiled
        adapter = LangGraphRuntimeAdapter(runner)
        handle = RunHandle(run_id="legacy-run", session_id="legacy", runtime_type="langgraph",
                           native_ref={key: ref.get(key, "") for key in (
                               "thread_id", "checkpoint_ns", "checkpoint_id",
                           )})
        await adapter.attach(handle)
        request = InteractionRequested(
            schema_version=2, event_id="legacy-request", seq=0, timestamp=1,
            run_id=handle.run_id, scope_id="legacy-scope", source=SourceRef(
                framework="langgraph", metadata={"agui_interaction_delivery": "legacy_approval"}
                if marked else {},
            ),
            interaction_id="gateway-approval", interaction_kind="approval",
            request=ApprovalRequest(kind="tool", call_id="gateway-approval", detail={
                "approval_request_id": "approval-1",
            }),
        )
        carrier = ItemCompleted(
            schema_version=2, event_id="legacy-tool-result", seq=0, timestamp=1,
            run_id=handle.run_id, scope_id="legacy-scope", source=SourceRef(framework="langgraph"),
            item_id="legacy-result", item_kind="tool_result", snapshot=ContentSnapshot(parts=(
                ToolResultContent(part_id="result", call_id="gateway-approval", result={
                    "type": "approval_required", "approval_request": {
                        "approval_request_id": "approval-1",
                    },
                }),
            )),
        )
        await adapter.restore_interaction_context(handle, [request], [request, carrier])
        assert not handle.native_ref.get("native_waits")
        native = request.model_copy(update={"source": request.source.model_copy(update={
            "metadata": {"agui_interaction_delivery": "native_interrupt"},
        })})
        with pytest.raises(ValueError, match="live native task"):
            await adapter.restore_interaction_context(handle, [native], [native, carrier])
        if not marked:
            with pytest.raises(ValueError, match="live native task"):
                await adapter.restore_interaction_context(handle, [request], [request])
        structured = request.model_copy(update={
            "interaction_kind": "structured_input", "request": StructuredInputRequest(schema={}),
        })
        with pytest.raises(ValueError, match="live native task"):
            await adapter.restore_interaction_context(handle, [structured], [structured])
        await runner.close()
