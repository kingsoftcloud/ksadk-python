from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.managed_runtime import _project_event
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.plugins.providers.harness_delegation import (
    DELEGATE_TASK_TOOL,
    HARNESS_CHILD_PROVIDER_REF,
    AdaptiveDelegationRuntime,
)
from ksadk.plugins.subagent_providers.codex import DEFAULT_CODEX_CHILD_PROVIDER_REF
from ksadk.plugins.subagents import (
    ChildHandle,
    SpawnSubagentRequest,
    SubagentEvent,
    SubagentProviderError,
    SubagentProviderRouter,
    SubagentResult,
    SubagentStatus,
)
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


class _DelegatingReasoner:
    def __init__(self) -> None:
        self.parent_turns = 0
        self.parent_tools: list[str] = []
        self.child_prompt = ""

    async def complete(self, *, model, prompt, messages, tools, **kwargs):  # noqa: ANN001
        del model, kwargs
        if "动态调度的通用子 Agent" in prompt:
            self.child_prompt = prompt
            return HarnessReasoningTurn(
                final_text="通用子任务完成", usage={"input_tokens": 2, "output_tokens": 1}
            )
        self.parent_turns += 1
        self.parent_tools = [tool.openai_schema["function"]["name"] for tool in tools]
        if self.parent_turns == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="research-one",
                        name=DELEGATE_TASK_TOOL,
                        arguments={
                            "task": "调研一个产品并给出证据摘要",
                            "task_kind": "general",
                            "label": "产品调研",
                        },
                    ),
                )
            )
        assert any(message.get("role") == "tool" for message in messages)
        return HarnessReasoningTurn(final_text="父 Agent 已汇总")


@pytest.mark.asyncio
async def test_managed_loop_dynamically_spawns_general_harness_child() -> None:
    reasoner = _DelegatingReasoner()
    delegation = AdaptiveDelegationRuntime(codex_available=False)
    engine = ManagedLangGraphEngine(
        reasoner=reasoner,
        checkpointer=memory_checkpointer(),
        delegation_runtime=delegation,
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://adaptive-agent@1",
        model=ModelBinding(profile_ref="model-profile://fixture@1"),
        prompt=PromptSpec(instructions="按需拆分任务。"),
    )
    compiled = await engine.compile(spec)
    handle = await engine.start(
        StartRequest(
            input="完成调研",
            user_id="user",
            session_id="session",
            agent_id="agent",
            metadata={"invocation_id": "adaptive-run"},
        ),
        compiled,
    )
    events = [event async for event in engine.stream(handle)]

    child_usage = [e for e in events if e.event_type == "usage.reported" and e.parent_run_id]
    assert len(child_usage) == 1
    assert engine._runs[handle.run_id].budget_usage["tokens"] == 3
    assert len([e for e in events if e.event_type == "run.started" and e.parent_run_id]) == 1
    from ksadk.harness.conformance import run_conformance_suite

    report = run_conformance_suite(events)
    assert report.ok, report.violations

    assert DELEGATE_TASK_TOOL in reasoner.parent_tools
    assert "六条以内的高质量证据" in reasoner.child_prompt
    assert "最后一轮必须" in reasoner.child_prompt
    progress = [
        event
        for event in events
        if event.event_type == EventType.RUN_PROGRESS
        and event.payload.get("kind") == "delegation.batch"
    ]
    assert [event.payload["status"] for event in progress] == ["running", "completed"]
    assert {event.payload["count"] for event in progress} == {1}
    assert {tuple(event.payload["labels"]) for event in progress} == {("产品调研",)}
    assert any(
        event.event_type == EventType.AGENT_STARTED and ":harness-research-one" in event.agent_id
        for event in events
    )
    assert any(
        event.event_type == EventType.TEXT_COMPLETED
        and event.payload.get("text") == "父 Agent 已汇总"
        for event in events
    )


class _ImmediateCodexProvider:
    def __init__(self) -> None:
        self.requests: list[SpawnSubagentRequest] = []
        self.disposed: list[str] = []

    async def describe(self) -> Mapping[str, Any]:
        return {}

    async def available(self) -> bool:
        return True

    async def spawn(self, request: SpawnSubagentRequest) -> ChildHandle:
        self.requests.append(request)
        return ChildHandle(
            handle_id="codex-child-1",
            provider_ref=request.provider_ref,
            parent_session_id=request.parent_session_id,
            parent_run_id=request.parent_run_id,
            child_session_id="codex-session",
            child_run_id="codex-run",
            depth=1,
            capabilities=("streaming",),
            capability_digest="sha256:" + "a" * 64,
            created_at=datetime.now(timezone.utc),
            resumable=False,
        )

    async def followup(self, handle: ChildHandle, input: Any) -> None:
        raise AssertionError((handle, input))

    async def status(self, handle: ChildHandle) -> SubagentStatus:
        return SubagentStatus(
            handle_id=handle.handle_id,
            state="succeeded",
            last_seq=2,
            updated_at=datetime.now(timezone.utc),
        )

    async def interrupt(self, handle: ChildHandle) -> None:
        raise AssertionError(handle)

    async def cancel(self, handle: ChildHandle) -> None:
        raise AssertionError(handle)

    def subscribe(self, handle: ChildHandle, *, after_seq: int = 0) -> AsyncIterator[SubagentEvent]:
        async def _events() -> AsyncIterator[SubagentEvent]:
            yield SubagentEvent(
                handle_id=handle.handle_id,
                event_id="codex-event-1",
                seq=1,
                kind="progress",
                payload={"state": "running"},
            )
            yield SubagentEvent(
                handle_id=handle.handle_id,
                event_id="codex-event-2",
                seq=2,
                kind="terminal",
                payload={"state": "succeeded"},
            )

        assert after_seq == 0
        return _events()

    async def result(self, handle: ChildHandle) -> SubagentResult:
        return SubagentResult(handle_id=handle.handle_id, state="succeeded", output="代码已修复")

    async def dispose(self, handle: ChildHandle) -> None:
        self.disposed.append(handle.handle_id)


@pytest.mark.asyncio
async def test_coding_task_is_forced_to_codex_and_emits_provider_progress() -> None:
    provider = _ImmediateCodexProvider()
    runtime = AdaptiveDelegationRuntime(
        router=SubagentProviderRouter({DEFAULT_CODEX_CHILD_PROVIDER_REF: provider}),
        codex_available=True,
    )
    parent = SimpleNamespace(
        handle=SimpleNamespace(run_id="parent-run"),
        state=SimpleNamespace(agent_id="agent", user_id="user", session_id="session"),
        seq=0,
        events=[],
        controller=None,
        control_events=[],
        observed_event_ids=set(),
    )
    route = runtime.preview_route(
        {"task": "修复 Python 代码并补测试", "task_kind": "general"},
        call_id="coding-one",
    )

    result, events = await runtime.invoke(
        engine=object(),
        parent_run=parent,
        # Even when the model says general, the platform detects coding signals.
        arguments={"task": "修复 Python 代码并补测试", "task_kind": "general"},
        call_id="coding-one",
    )

    assert result["provider"] == "codex"
    assert result["output"] == "代码已修复"
    assert provider.requests[0].provider_ref == DEFAULT_CODEX_CHILD_PROVIDER_REF
    assert provider.requests[0].policy.max_steps == 8
    assert provider.disposed == ["codex-child-1"]
    assert route == {
        "kind": "delegation.route",
        "status": "in_progress",
        "call_id": "coding-one",
        "label": "修复 Python 代码并补测试",
        "task_kind": "coding",
        "provider_ref": DEFAULT_CODEX_CHILD_PROVIDER_REF,
        "reason": "coding_signal",
    }
    assert [event.payload["status"] for event in events] == ["running", "succeeded"]
    assert parent.events[0].payload == {
        "kind": "subagent.event",
        "status": "running",
        "call_id": "coding-one",
        "label": "修复 Python 代码并补测试",
        "provider_ref": DEFAULT_CODEX_CHILD_PROVIDER_REF,
        "child_event_kind": "lifecycle",
    }


def test_non_coding_task_defaults_to_harness_and_simple_task_can_skip_tool() -> None:
    runtime = AdaptiveDelegationRuntime(codex_available=False)
    decision = runtime.route("比较三款产品并总结官方资料")
    assert decision.provider_ref == HARNESS_CHILD_PROVIDER_REF
    assert decision.task_kind == "general"
    hinted = runtime.route("调研 Codex 的长任务能力", "coding")
    assert hinted.provider_ref == HARNESS_CHILD_PROVIDER_REF
    assert hinted.reason == "coding_hint_without_signal"
    implementation_research = runtime.route("调研 Codex 的 checkpoint 实现和恢复机制", "coding")
    assert implementation_research.provider_ref == HARNESS_CHILD_PROVIDER_REF
    source_research = runtime.route("查看三个项目的 source code and repository docs", "coding")
    assert source_research.provider_ref == HARNESS_CHILD_PROVIDER_REF
    constrained_research = runtime.route(
        "Search official docs. Do not write any code or files; just report findings.",
        "coding",
    )
    assert constrained_research.provider_ref == HARNESS_CHILD_PROVIDER_REF
    chinese_constrained = runtime.route("调研官方资料，不要写代码或修改文件", "coding")
    assert chinese_constrained.provider_ref == HARNESS_CHILD_PROVIDER_REF
    assert "Do not delegate a simple task" in runtime.openai_schema["function"]["description"]


@pytest.mark.asyncio
async def test_codex_delegation_cannot_bypass_host_execution_policy():
    from ksadk.harness.execution_policy import ExecutionPolicy

    provider = _ImmediateCodexProvider()
    runtime = AdaptiveDelegationRuntime(
        router=SubagentProviderRouter({DEFAULT_CODEX_CHILD_PROVIDER_REF: provider}),
        codex_available=True,
    )
    parent = SimpleNamespace(
        handle=SimpleNamespace(run_id="parent-run"),
        execution_policy=ExecutionPolicy(),
    )
    with pytest.raises(SubagentProviderError) as error:
        await runtime.invoke(
            engine=object(), parent_run=parent, call_id="coding",
            arguments={"task": "修复 Python 代码", "task_kind": "coding"},
        )
    assert error.value.code == "execution_policy_unsupported"
    assert provider.requests == []


def test_delegation_progress_projects_to_compact_summary_and_expandable_detail() -> None:
    running = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "kind": "delegation.batch",
            "status": "running",
            "count": 3,
            "labels": ["调研 Codex", "调研 DSH", "调研 ADK"],
        },
    )
    completed = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=2,
        payload={
            "kind": "delegation.batch",
            "status": "completed",
            "count": 3,
            "labels": ["调研 Codex", "调研 DSH", "调研 ADK"],
        },
    )

    started_projection = _project_event(running)
    completed_projection = _project_event(completed)

    assert [item.item_kind for item in started_projection] == [
        "message",
        "message",
        "reasoning",
        "reasoning",
    ]
    assert (
        started_projection[0].initial.parts[0].text
        == "3 个子智能体正在运行"
    )
    assert (
        started_projection[2].initial.parts[0].text
        == "• 正在运行：调研 Codex\n• 正在运行：调研 DSH\n• 正在运行：调研 ADK"
    )
    assert (
        completed_projection[1].snapshot.parts[0].text
        == "3 个子智能体已完成，正在整理结果"
    )
    assert (
        completed_projection[3].snapshot.parts[0].text
        == "• 已完成：调研 Codex\n• 已完成：调研 DSH\n• 已完成：调研 ADK"
    )
    assert started_projection[0].item_id == completed_projection[0].item_id
    assert started_projection[2].item_id == completed_projection[2].item_id


def test_provider_retry_is_public_but_redacts_provider_details() -> None:
    event = RuntimeEvent.create(
        EventType.MODEL_CALL_FAILED,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "model": "secret-provider-model",
            "error": "raw provider error",
            "failure_category": "rate_limit",
            "action": "retry_same_model",
            "model_attempt": 1,
            "retry_delay_ms": 1000,
        },
    )

    projected = _project_event(event)

    assert [item.item_kind for item in projected] == ["message", "message", "message"]
    text = projected[-1].snapshot.parts[0].text
    assert text == "模型服务繁忙，1 秒后自动重试（第 2/3 次）"
    assert "secret-provider-model" not in text
    assert "raw provider error" not in text


def test_subagent_provider_retry_uses_declared_next_attempt() -> None:
    event = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "kind": "provider.retry",
            "status": "waiting",
            "label": "调研 Codex",
            "next_attempt": 2,
            "max_attempts": 3,
            "delay_ms": 1000,
        },
    )

    projected = _project_event(event)

    assert projected[-1].snapshot.parts[0].text == (
        "模型服务繁忙（调研 Codex），1 秒后自动重试（第 2/3 次）"
    )


def test_tool_batch_projects_human_activity_without_arguments() -> None:
    event = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "kind": "tool.batch",
            "status": "running",
            "count": 2,
            "tools": ["web_search", "web_fetch"],
        },
    )

    projected = _project_event(event)

    assert projected[0].initial.parts[0].text == "正在搜索资料"
    assert "query" not in projected[0].initial.parts[0].text


def test_private_reasoning_and_internal_delegation_tool_stay_out_of_chat() -> None:
    reasoning = RuntimeEvent.create(
        EventType.REASONING_COMPLETED,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={"text": "不应逐字展示的模型推理"},
    )
    tool = RuntimeEvent.create(
        EventType.TOOL_CALL_BEGIN,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=2,
        payload={
            "call_id": "delegate-1",
            "name": DELEGATE_TASK_TOOL,
            "args": {"task": "不应在聊天区展开的子任务"},
        },
    )

    assert [item.item_kind for item in _project_event(reasoning)] == ["status"]
    assert [item.item_kind for item in _project_event(tool)] == ["status"]


def test_ordinary_tool_payload_stays_in_trace_only() -> None:
    tool = RuntimeEvent.create(
        EventType.TOOL_CALL_BEGIN,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=2,
        payload={
            "call_id": "search-1",
            "name": "web_search",
            "args": {"query": "private detailed query", "api_key": "must-not-render"},
        },
    )

    projected = _project_event(tool)

    assert [item.item_kind for item in projected] == ["status"]
    assert "must-not-render" not in projected[0].snapshot.parts[0].text


def test_individual_subagent_progress_stays_in_trace_only() -> None:
    event = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "kind": "subagent.event",
            "status": "running",
            "label": "调研 Codex",
        },
    )

    assert [item.item_kind for item in _project_event(event)] == ["status"]


def test_plan_progress_is_compact() -> None:
    event = RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent",
        user_id="user",
        session_id="session",
        invocation_id="run",
        seq_id=1,
        payload={
            "kind": "plan",
            "status": "in_progress",
            "message": "第一阶段：检索资料\n" + "详细说明" * 80,
        },
    )

    text = _project_event(event)[2].snapshot.parts[0].text

    assert text.startswith("计划：第一阶段：检索资料；")
    assert text.endswith("…")
    assert len(text) <= 123


@pytest.mark.asyncio
async def test_coding_task_never_silently_falls_back_when_codex_is_missing() -> None:
    runtime = AdaptiveDelegationRuntime(codex_available=False)
    parent = SimpleNamespace(
        handle=SimpleNamespace(run_id="parent-run"),
        state=SimpleNamespace(agent_id="agent", user_id="user", session_id="session"),
    )
    with pytest.raises(SubagentProviderError, match="requires the Codex child provider") as exc:
        await runtime.invoke(
            engine=object(),
            parent_run=parent,
            arguments={"task": "implement a repository patch", "task_kind": "auto"},
            call_id="coding-missing",
        )
    assert exc.value.code == "codex_subagent_unavailable"


@pytest.mark.asyncio
async def test_dynamic_child_approval_restores_without_consuming_another_child_slot():
    calls = []

    async def write(arguments, call_id):
        calls.append(call_id)
        return "written"

    class Reasoner:
        async def complete(self, *, prompt, messages, **kwargs):
            if any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(final_text="done")
            if "动态调度的通用子 Agent" in prompt:
                call = HarnessToolCall("write-call", "write", {})
            else:
                call = HarnessToolCall("delegate", DELEGATE_TASK_TOOL, {
                    "task": "保存已核对的调研摘要", "task_kind": "general", "label": "保存摘要",
                })
            return HarnessReasoningTurn(tool_calls=(call,))

    saver = memory_checkpointer()
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://adaptive@1",
        model=ModelBinding(profile_ref="model-profile://fixture@1"),
        prompt=PromptSpec(instructions="parent"),
    )

    def make():
        return ManagedLangGraphEngine(
            reasoner=Reasoner(), checkpointer=saver, tools={"write": write},
            approval_required={"write"},
            delegation_runtime=AdaptiveDelegationRuntime(codex_available=False, max_children=1),
        )

    first = make()
    handle = await first.start(StartRequest(
        input="go", user_id="user", agent_id="agent", session_id="session",
    ), await first.compile(spec))
    events = [e async for e in first.stream(handle)]
    assert (await first.snapshot_state(handle)).status.value == "awaiting_approval"
    assert not calls
    second = make()
    await second.attach(handle, await second.compile(spec))
    await second.resume(
        handle, ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
        ResumePayload(kind="approval_decision", call_id="delegate", data="approved"),
    )
    events += [e async for e in second.stream(handle)]
    assert calls == ["write-call"]
    assert (await second.snapshot_state(handle)).status.value == "completed"
    assert second._runs[handle.run_id].dynamic_calls == {"delegate"}
    assert len([e for e in events if e.event_type == "run.started" and e.parent_run_id]) == 1
