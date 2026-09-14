from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.engine.langgraph import (
    ManagedLangGraphEngine,
    memory_checkpointer,
    sqlite_checkpointer,
)
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.harness.tools import HarnessTool
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


def spec(*, config=None, children=None):
    return HarnessSpec(
        agent_revision_ref="agent-revision://governance@1",
        model=ModelBinding(profile_ref="model-profile://fake@1"),
        prompt=PromptSpec(instructions="parent"),
        sub_agents=tuple(
            children
            or [
                SubAgentBinding(
                    name="child",
                    instructions="child",
                    tools=("write",),
                    inherit_skills=False,
                )
            ]
        ),
        execution_strategy=ExecutionStrategySpec(config=config or {}),
    )


def tool(name, handler):
    return HarnessTool(
        name=name, description=name, parameters={"type": "object"}, handler=handler, source="test"
    )


def request():
    return StartRequest(
        input="go",
        user_id="user",
        agent_id="parent",
        session_id="session",
        metadata={"invocation_id": "parent-run"},
    )


class DelegateReasoner:
    async def complete(self, *, prompt, messages, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            return HarnessReasoningTurn(
                final_text="done", usage={"input_tokens": 2, "output_tokens": 1}
            )
        if prompt == "child":
            return HarnessReasoningTurn(
                tool_calls=(HarnessToolCall("write-call", "write", {}),),
                usage={"input_tokens": 2, "output_tokens": 1},
            )
        return HarnessReasoningTurn(
            tool_calls=(HarnessToolCall("delegate", "child", {"task": "work"}),),
            usage={"input_tokens": 2, "output_tokens": 1},
        )


async def collect(engine, handle):
    async with asyncio.timeout(5):
        return [event async for event in engine.stream(handle)]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("approved", 1), ("denied", 0)])
async def test_child_inherits_approval_and_resumes_real_run(decision, expected):
    calls = []

    async def write(arguments, call_id):
        calls.append(arguments)
        return "written"

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
        approval_required={"write"},
        tenant_id="tenant",
    )
    compiled = await engine.compile(spec())
    handle = await engine.start(request(), compiled)
    events = await collect(engine, handle)
    assert calls == []
    assert (await engine.snapshot_state(handle)).status.value == "awaiting_approval"
    approval = next(
        e for e in events if e.event_type == "approval.requested" and not e.parent_run_id
    )
    detail = approval.payload["detail"]
    assert detail["child_run_id"] == "parent-run:sub:child:delegate"
    assert detail["child_call_id"] == "write-call"
    assert detail["child_handle"]["native_ref"]["thread_id"].startswith("tenant:tenant/")
    await engine.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
        ResumePayload(kind="approval_decision", call_id="delegate", data=decision),
    )
    events += await collect(engine, handle)
    assert len(calls) == expected
    assert (await engine.snapshot_state(handle)).status.value == "completed"
    child_starts = [e for e in events if e.event_type == "run.started" and e.parent_run_id]
    assert len(child_starts) == 1
    assert any(e.event_type == "run.completed" and e.parent_run_id for e in events)
    assert [e.seq_id for e in events] == sorted({e.seq_id for e in events})


@pytest.mark.asyncio
async def test_child_events_are_live_and_parent_cancel_awaits_child_terminal():
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(arguments, call_id):
        entered.set()
        await release.wait()
        return "written"

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
    )
    handle = await engine.start(request(), await engine.compile(spec()))
    events = []

    async def consume():
        async for event in engine.stream(handle):
            events.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 3)
    for _ in range(30):
        if any(e.event_type == "tool.call.begin" and e.parent_run_id for e in events):
            break
        await asyncio.sleep(0)
    assert any(e.event_type == "run.started" and e.parent_run_id for e in events)
    assert any(e.event_type == "tool.call.begin" and e.parent_run_id for e in events)
    assert not release.is_set()
    await engine.cancel(handle)
    await asyncio.wait_for(consumer, 3)
    terminals = [e for e in events if e.event_type == "run.canceled"]
    assert len(terminals) == 2
    assert terminals[0].parent_run_id == handle.run_id
    assert terminals[1].parent_run_id is None
    assert not engine._active_subagent_runs


@pytest.mark.asyncio
async def test_durable_child_approval_recovery_and_prior_sibling_not_replayed(tmp_path):
    calls = []

    async def write(arguments, call_id):
        calls.append("write")
        return "written"

    class BatchReasoner(DelegateReasoner):
        async def complete(self, *, prompt, messages, **kwargs):
            if prompt == "parent" and not any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall("first", "safe", {}),
                        HarnessToolCall("delegate", "child", {"task": "work"}),
                    )
                )
            return await super().complete(prompt=prompt, messages=messages, **kwargs)

    async def safe(arguments, call_id):
        calls.append("safe")
        return "read"

    async with sqlite_checkpointer(str(tmp_path / "checkpoints.sqlite")) as saver:

        def make():
            return ManagedLangGraphEngine(
                reasoner=BatchReasoner(),
                checkpointer=saver,
                tools={"write": tool("write", write), "safe": tool("safe", safe)},
                approval_required={"write"},
                tenant_id="tenant",
            )

        first = make()
        handle = await first.start(request(), await first.compile(spec()))
        await collect(first, handle)
        assert calls == ["safe"]
        second = make()
        await second.attach(handle, await second.compile(spec()))
        assert second._runs[handle.run_id].pending_approval["child_call_id"] == "write-call"
        await second.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(kind="approval_decision", call_id="delegate", data="approved"),
        )
        events = await collect(second, handle)
        assert calls == ["safe", "write"]
        assert (await second.snapshot_state(handle)).status.value == "completed", events


@pytest.mark.asyncio
async def test_cancel_pending_approval_emits_both_terminal_states():
    async def write(arguments, call_id):
        pytest.fail("unapproved write")

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
        approval_required={"write"},
    )
    handle = await engine.start(request(), await engine.compile(spec()))
    await collect(engine, handle)
    await engine.cancel(handle)
    events = await collect(engine, handle)
    assert len([e for e in events if e.event_type == "run.canceled"]) == 2
    assert (await engine.snapshot_state(handle)).status.value == "canceled"


@pytest.mark.asyncio
async def test_child_budget_consumes_parent_budget_before_side_effect():
    calls = []

    async def write(arguments, call_id):
        calls.append(1)
        return "written"

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
    )
    handle = await engine.start(request(), await engine.compile(spec(config={"max_tool_calls": 1})))
    events = await collect(engine, handle)
    assert calls == []  # Parent delegation itself has consumed the only call.
    assert any(e.payload.get("error_category") == "budget_exhausted" for e in events)
    assert engine._runs[handle.run_id].budget_usage["tools"] == 1


@pytest.mark.asyncio
async def test_canonical_child_lifecycle_does_not_terminate_root():
    async def write(arguments, call_id):
        return "written"

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
    )
    adapter = ManagedHarnessRuntimeAdapter(spec(), engine=engine)
    handle = await adapter.start(request())
    events = [event async for event in adapter.stream(handle)]
    assert len([e for e in events if e.event_type == "run.completed"]) == 1
    child_terminal = [
        e
        for e in events
        if e.source.metadata["native_event_type"] == "run.completed"
        and e.source.metadata.get("parent_run_id")
    ]
    assert child_terminal[0].event_type == "item.completed"
    assert child_terminal[0].source.native_run_id == "parent-run:sub:child:delegate"
    assert child_terminal[0].run_id == handle.run_id


@pytest.mark.asyncio
async def test_second_child_approval_never_reuses_first_decision():
    calls = []

    async def write(arguments, call_id):
        calls.append(arguments["n"])
        return "written"

    class TwoWrites(DelegateReasoner):
        async def complete(self, *, prompt, messages, **kwargs):
            if prompt == "child" and not any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall("write-1", "write", {"n": 1}),
                        HarnessToolCall("write-2", "write", {"n": 2}),
                    )
                )
            return await super().complete(prompt=prompt, messages=messages, **kwargs)

    saver = memory_checkpointer()

    def make():
        return ManagedLangGraphEngine(
            reasoner=TwoWrites(),
            checkpointer=saver,
            tools={"write": tool("write", write)},
            approval_required={"write"},
        )

    engine = make()
    handle = await engine.start(request(), await engine.compile(spec()))
    await collect(engine, handle)
    for expected_call, decision, expected_writes in [
        ("write-1", "approved", [1]),
        ("write-2", "denied", [1]),
    ]:
        assert engine._runs[handle.run_id].pending_approval["child_call_id"] == expected_call
        # Each decision is processed by a newly attached engine.
        engine = make()
        await engine.attach(handle, await engine.compile(spec()))
        await engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(kind="approval_decision", call_id="delegate", data=decision),
        )
        await collect(engine, handle)
        assert calls == expected_writes
    assert (await engine.snapshot_state(handle)).status.value == "completed"


@pytest.mark.asyncio
async def test_timeout_cancels_native_child_task():
    stopped = asyncio.Event()

    async def write(arguments, call_id):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
    )
    handle = await engine.start(
        request(),
        await engine.compile(
            spec(
                children=[
                    SubAgentBinding(
                        name="child",
                        instructions="child",
                        tools=("write",),
                        timeout_seconds=0.02,
                    )
                ]
            )
        ),
    )
    events = await collect(engine, handle)
    assert stopped.is_set()
    assert any(e.payload.get("error_category") == "timeout" for e in events)
    assert any(e.event_type == "run.canceled" and e.parent_run_id for e in events)


@pytest.mark.asyncio
async def test_child_inherits_capability_policy_and_authorization_identity():
    from ksadk.harness.capability_runtime import CapabilityRuntime
    from ksadk.harness.tool_policy import ToolPolicy

    identities = []

    class ParentPolicy(ToolPolicy):
        def decide(self, context):
            identities.append((context.tool_name, context.tenant_id, context.agent_id))
            return super().decide(context)

    async def write(arguments, call_id):
        pytest.fail("parent policy denies child write")

    runtime = CapabilityRuntime(policy=ParentPolicy(denied_prefixes=("write",)))
    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tenant_id="tenant",
        capability_runtime=runtime,
        tools={"write": tool("write", write)},
    )
    handle = await engine.start(request(), await engine.compile(spec()))
    events = await collect(engine, handle)
    assert ("write", "tenant", "parent") in identities
    assert any("policy-denied" in str(e.payload.get("error")) for e in events)


@pytest.mark.asyncio
async def test_static_approval_remains_required_when_capability_runtime_allows():
    from ksadk.harness.capability_runtime import CapabilityRuntime

    async def write(arguments, call_id):
        pytest.fail("static approval is an additional constraint")

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        capability_runtime=CapabilityRuntime(),
        tools={"write": tool("write", write)},
        approval_required={"write"},
    )
    handle = await engine.start(request(), await engine.compile(spec()))
    await collect(engine, handle)
    assert (await engine.snapshot_state(handle)).status.value == "awaiting_approval"


@pytest.mark.asyncio
async def test_completed_child_checkpoint_returns_result_without_reexecution():
    calls = []

    async def write(arguments, call_id):
        calls.append(1)
        return "written"

    engine = ManagedLangGraphEngine(
        reasoner=DelegateReasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
    )
    handle = await engine.start(request(), await engine.compile(spec()))
    run = engine._runs[handle.run_id]
    first = await engine._invoke_tool("child", {"task": "work"}, run=run, call_id="delegate")
    second = await engine._invoke_tool("child", {"task": "work"}, run=run, call_id="delegate")
    assert calls == [1]
    assert second["evidence"]["child_run_id"] == first["evidence"]["child_run_id"]
    assert second["evidence"]["recovered_from_checkpoint"]
    assert second["output"] == first["output"]


@pytest.mark.asyncio
async def test_explicitly_pure_text_children_retain_parallel_execution():
    entered = set()
    release = asyncio.Event()

    class ParallelReasoner:
        async def complete(self, *, prompt, messages, **kwargs):
            if prompt in {"one", "two"}:
                entered.add(prompt)
                if len(entered) == 2:
                    release.set()
                await asyncio.wait_for(release.wait(), 2)
                return HarnessReasoningTurn(final_text=prompt)
            if any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(final_text="joined")
            return HarnessReasoningTurn(
                tool_calls=tuple(
                    HarnessToolCall(name, name, {"task": name}) for name in ("one", "two")
                )
            )

    engine = ManagedLangGraphEngine(reasoner=ParallelReasoner(), checkpointer=memory_checkpointer())
    handle = await engine.start(
        request(),
        await engine.compile(
            spec(
                children=[
                    SubAgentBinding(name=name, instructions=name, inherit_skills=False)
                    for name in ("one", "two")
                ]
            )
        ),
    )
    events = await collect(engine, handle)
    assert entered == {"one", "two"}
    assert (await engine.snapshot_state(handle)).status.value == "completed"
    assert len([e for e in events if e.event_type == "run.completed" and e.parent_run_id]) == 2
    assert [e.seq_id for e in events] == sorted({e.seq_id for e in events})


@pytest.mark.asyncio
async def test_restored_pending_child_can_be_canceled_without_resuming_tool():
    async def write(arguments, call_id):
        pytest.fail("cancel must not execute a pending tool")

    saver = memory_checkpointer()

    def make():
        return ManagedLangGraphEngine(
            reasoner=DelegateReasoner(),
            checkpointer=saver,
            tools={"write": tool("write", write)},
            approval_required={"write"},
        )

    first = make()
    handle = await first.start(request(), await first.compile(spec()))
    await collect(first, handle)
    second = make()
    await second.attach(handle, await second.compile(spec()))
    await second.cancel(handle)
    events = await collect(second, handle)
    assert len([e for e in events if e.event_type == "run.canceled"]) == 2
    assert not second._active_subagent_runs
