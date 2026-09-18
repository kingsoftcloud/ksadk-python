# -*- coding: utf-8 -*-
"""LangGraph checkpoint resume 回包（Kernel Task 6 Step 2/6）。

断言结构化 response 映射为 request 时存的 checkpoint/thread target，
经 ``adapter.resume()`` 恢复**同一 thread**（time-travel），不是新 run：
- resume target = 存的 checkpoint_id（native_target），thread 落在
  handle.native_ref / native_target；
- payload 携带原 interrupt call_id 与完整 response；
- Worker 分发路径：resume 接受后才写 InteractionResolved，且后续 stream
  消费使用 resume 返回的新 handle。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from ksadk.events.canonical import RunProgress, RunStarted, SourceRef
from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.provider import InteractionResolveContext
from ksadk.interaction.providers.langgraph import LangGraphInteractionProvider
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.state import RunState
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, RunHandle
from tests.interaction.test_provider_dispatch import (
    LangGraphLikeAdapter,
    _request_interaction,
    _seed_active_run,
)
from tests.kernel.control_harness import AGENT, command, kernel_stack


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = 2.0
) -> None:
    """让出事件循环直到 background stream 任务达成条件（确定性收口）。"""

    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0)


def _resumed_stream_events(run_id: str) -> list:
    """resume 后新 handle 的 stream 产出的 runtime/v2 事件。"""

    def _src() -> SourceRef:
        return SourceRef(framework="langgraph")

    return [
        RunStarted(
            schema_version=2,
            event_id=f"{run_id}-started",
            seq=0,
            timestamp=1780000000.0,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            source=_src(),
        ),
        RunProgress(
            schema_version=2,
            event_id=f"{run_id}-progress",
            seq=0,
            timestamp=1780000000.5,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            progress=0.5,
            source=_src(),
        ),
    ]


def _record(native_target: dict) -> InteractionRecord:
    return InteractionRecord(
        interaction_id="it-lg-1",
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
        run_id="run-durable-1",
        kind="approval",
        request_schema={"type": "object"},
        created_at=datetime.now(UTC).isoformat(),
        provider_id="langgraph",
        native_target=native_target,
    )


class _ResumeRecordingAdapter(LangGraphLikeAdapter):
    """独立实例化的 resume 记录 stub（provider 级测试用）。"""


async def test_resume_receives_stored_checkpoint_and_thread_target():
    adapter = _ResumeRecordingAdapter()
    handle = RunHandle(run_id="lg-run-1", session_id="s1", runtime_type="langgraph")
    handle.native_ref["thread_id"] = "thread-42"
    record = _record(
        {"checkpoint_id": "ckpt-9", "thread_id": "thread-42", "call_id": "interr-3"}
    )
    submission = InteractionSubmission(
        interaction_id="it-lg-1",
        expected_revision=1,
        action="approve",
        response={"approved": True, "note": "ok"},
        idempotency_key="idem-lg-1",
    )
    provider = LangGraphInteractionProvider()
    resumed = await provider.resolve(
        InteractionResolveContext(
            adapter=adapter, handle=handle, activation_id="act-1", fencing_token=3
        ),
        record,
        submission,
    )
    assert len(adapter.resumes) == 1
    resumed_handle, target, payload = adapter.resumes[0]
    assert resumed_handle is handle
    assert target == ResumeTarget(kind="checkpoint_id", id="ckpt-9")
    assert payload is not None
    assert payload.kind == "approval_decision"
    assert payload.call_id == "interr-3"
    assert payload.data == {"approved": True, "note": "ok"}
    # thread target 也落到 handle（LangGraph resume 的 thread 语义）。
    assert handle.native_ref["thread_id"] == "thread-42"
    # durable_resume 返回 resume 后的新 handle（同一 thread 的时间旅行）。
    assert resumed is adapter.resumed_handle


async def test_worker_dispatch_resumes_stored_checkpoint_then_resolves():
    stack = await kernel_stack(adapter=LangGraphLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-lg-w",
        provider_id="langgraph",
        run_id=run_id,
        native_target={
            "checkpoint_id": "ckpt-w",
            "thread_id": "thread-w",
            "call_id": "interr-w",
        },
    )
    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-lg",
            payload={
                "run_id": run_id,
                "interaction_id": "it-lg-w",
                "token_ref": "tok-lg",
                "response": {"approved": True},
                "action": "approve",
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    # resume 后的新 handle 的 stream 要产出 fenced runtime/v2 事实。
    adapter.stream_events = _resumed_stream_events("lg-resumed")
    adapter.stream_error = None  # resume 后 stream 自然结束
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"

    # resume 用存的 checkpoint target + 原 interrupt call_id + 完整 response。
    assert len(adapter.resumes) == 1
    _, target, payload = adapter.resumes[0]
    assert target == ResumeTarget(kind="checkpoint_id", id="ckpt-w")
    assert payload is not None
    assert payload.call_id == "interr-w"
    assert payload.data == {"approved": True}

    # resume 接受后才写 InteractionResolved。
    record = await stack.store.get("it-lg-w")
    assert record is not None and record.status == "resolved"
    events = await stack.events.read("s1", 0, 200)
    assert [e for e in events if e.event_type == "interaction.resolved"]

    # resume 返回的新 handle 被 background stream 真正消费（同 thread 续跑），
    # 且事件以 durable run id 落为 family=runtime/v2 的 fenced 事实。
    async def _stream_consumed() -> bool:
        return "lg-resumed" in adapter.streams

    async def _run_finished() -> bool:
        run = await stack.store.load_run(run_id)
        return run is not None and run.state == RunState.COMPLETED

    await _wait_until(_stream_consumed)
    await _wait_until(_run_finished)
    run = await stack.store.load_run(run_id)
    assert run is not None and run.state == RunState.COMPLETED
    events = await stack.events.read("s1", 0, 200)
    runtime_events = [e for e in events if e.family == "runtime"]
    assert runtime_events
    assert all(e.run_id == run_id for e in runtime_events)
    assert {"run.started", "run.progress"} <= {e.event_type for e in runtime_events}


async def test_missing_checkpoint_target_is_typed_rejection():
    stack = await kernel_stack(adapter=LangGraphLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-lg-bad",
        provider_id="langgraph",
        run_id=run_id,
        native_target={"call_id": "interr-bad"},  # 没存 checkpoint
    )
    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-lg-bad",
            payload={
                "run_id": run_id,
                "interaction_id": "it-lg-bad",
                "token_ref": "tok-lg-bad",
                "response": {"approved": True},
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    result = await worker.run_once(AGENT, lease)
    # ValueError 属未知异常：terminal_failure，消息保持 claimed，绝不 resolved。
    assert result.outcome == "terminal_failure"
    assert adapter.resumes == []
    record = await stack.store.get("it-lg-bad")
    assert record is not None and record.status == "pending"
