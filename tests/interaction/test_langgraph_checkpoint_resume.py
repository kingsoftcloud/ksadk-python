# -*- coding: utf-8 -*-
"""LangGraph checkpoint resume 回包（Phase 1 Task 6 Step 2/6）。

断言结构化 response 映射为 request 时存的 checkpoint/thread target，
经 ``adapter.resume()`` 恢复**同一 thread**（time-travel），不是新 run：
- resume target = 存的 checkpoint_id（native_target），thread 落在
  handle.native_ref / native_target；
- payload 携带原 interrupt call_id 与完整 response；
- Worker 分发路径：resume 接受后才写 InteractionResolved，且后续 stream
  消费使用 resume 返回的新 handle。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.provider import InteractionResolveContext
from ksadk.interaction.providers.langgraph import LangGraphInteractionProvider
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import AgentKernelError
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, RunHandle
from tests.interaction.test_provider_dispatch import (
    LangGraphLikeAdapter,
    _request_interaction,
    _seed_active_run,
)
from tests.kernel.control_harness import AGENT, command, kernel_stack


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

    # resume 返回的新 handle 被用于后续 stream 消费（同 thread 续跑）。
    assert "lg-resumed" in adapter.streams


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


def _unused_guard() -> ActivationWriteGuard:  # pragma: no cover - typing helper
    return ActivationWriteGuard(activation_id="act-1", fencing_token=1)


def _unused_error() -> AgentKernelError:  # pragma: no cover - typing helper
    return AgentKernelError("unsupported", "unused")
