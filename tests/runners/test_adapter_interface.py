"""RuntimeAdapter 签名级测试 (goal-03)。

只验证签名与语义契约,不验证具体 adapter 实现(那是 A4/A6):
- 六动词签名(start/stream/cancel/resume/checkpoint/close)。
- CancelResult 四值状态机(不是 bool)。
- ResumeTarget / ResumePayload 分离。
- CheckpointCapability 粒度声明。
- StartRequest 带 session/tenant 维度。
- stream 返回 RuntimeEvent 事件流(对接 G0.2)。
- RuntimeRegistry 注册/查找/实例化/错误路径。
"""

from __future__ import annotations

import inspect
from typing import AsyncIterator

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)


class _FakeRuntime(BaseRuntime):
    runtime_type = "fake"

    def native_capabilities(self) -> dict:
        return {"cancel": True, "checkpoint": False}


class _FakeAdapter(RuntimeAdapter):
    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id="inv_1",
            session_id=request.session_id,
            runtime_type="fake",
            native_ref={},
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        yield RuntimeEvent.create(
            EventType.RUN_STARTED,
            agent_id="a",
            user_id="u",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=1,
            payload={"status": "in_progress"},
        )

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self, handle, target: ResumeTarget, payload: ResumePayload | None
    ) -> RunHandle:
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id="ck_1",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        return None


# ---- 六动词签名 ----

EXPECTED_VERBS = {"start", "stream", "cancel", "resume", "checkpoint", "close"}


def test_six_verbs_present_and_abstract():
    for verb in EXPECTED_VERBS:
        assert hasattr(RuntimeAdapter, verb), verb
        assert getattr(RuntimeAdapter, verb).__isabstractmethod__ is True


def test_stream_returns_runtime_event_iterator():
    sig = inspect.signature(RuntimeAdapter.stream)
    assert "RuntimeEvent" in str(sig.return_annotation)


def test_resume_takes_separate_target_and_payload():
    params = list(inspect.signature(RuntimeAdapter.resume).parameters)
    assert params == ["self", "handle", "target", "payload"]
    sig = inspect.signature(RuntimeAdapter.resume)
    assert "ResumeTarget" in str(sig.parameters["target"].annotation)
    assert "ResumePayload" in str(sig.parameters["payload"].annotation)


# ---- CancelResult ----


def test_cancel_result_is_four_state_enum_not_bool():
    values = {member.value for member in CancelResult}
    assert values == {
        "interrupted_active_turn",
        "pending_cancel_recorded",
        "not_running",
        "failed",
    }
    assert len(list(CancelResult)) == 4


# ---- ResumeTarget / ResumePayload ----


def test_resume_target_kinds():
    for kind in ("invocation_id", "thread_id", "checkpoint_id"):
        target = ResumeTarget(kind=kind, id="x")
        assert target.kind == kind


def test_resume_payload_kinds_and_optional_call_id():
    payload = ResumePayload(kind="approval_decision", call_id="ap_1", data={"d": "approved"})
    assert payload.kind == "approval_decision"
    assert payload.call_id == "ap_1"
    # payload 可只给 kind(空回包)
    minimal = ResumePayload(kind="free_text")
    assert minimal.call_id is None and minimal.data is None


def test_checkpoint_capability_fields():
    cap = CheckpointCapability(
        supported=True,
        granularity="snapshot",
        rollback_scope="invocation",
        fork_supported=True,
        durable=True,
        shared_across_pods=False,
        reason="test",
    )
    assert cap.granularity == "snapshot"
    assert cap.rollback_scope == "invocation"
    assert cap.fork_supported is True


def test_start_request_has_session_tenant_dimensions():
    req = StartRequest(input="hi", user_id="u1", session_id="s1")
    assert req.user_id == "u1"
    assert req.session_id == "s1"
    assert req.agent_id is None and req.model is None
    assert req.config == {} and req.metadata == {}


# ---- RuntimeRegistry ----


def test_registry_register_get_create():
    registry = RuntimeRegistry()
    registry.register("fake", _FakeAdapter)
    assert registry.get("fake") is _FakeAdapter
    adapter = registry.create("fake", _FakeRuntime())
    assert isinstance(adapter, _FakeAdapter)
    assert "fake" in registry.registered_types()


def test_registry_rejects_invalid():
    registry = RuntimeRegistry()
    with pytest.raises(TypeError):
        registry.register("bad", object)  # 非 RuntimeAdapter 子类
    with pytest.raises(ValueError):
        registry.register("  ", _FakeAdapter)
    with pytest.raises(KeyError):
        registry.get("missing")


# ---- 接口可被驱动(证明可实现) ----


@pytest.mark.asyncio
async def test_adapter_can_be_driven_end_to_end():
    adapter = _FakeAdapter(_FakeRuntime())
    handle = await adapter.start(StartRequest(input="hi", user_id="u", session_id="s"))
    assert handle.run_id and handle.session_id == "s"

    events = [event async for event in adapter.stream(handle)]
    assert len(events) == 1
    assert isinstance(events[0], RuntimeEvent)
    assert events[0].event_type == EventType.RUN_STARTED

    assert await adapter.cancel(handle) is CancelResult.INTERRUPTED_ACTIVE_TURN

    resumed = await adapter.resume(
        handle,
        ResumeTarget(kind="invocation_id", id="inv_1"),
        ResumePayload(kind="approval_decision", call_id="ap_1"),
    )
    assert resumed.run_id == handle.run_id

    descriptor = await adapter.checkpoint(handle)
    assert descriptor.capability.supported is False

    await adapter.close(handle)
