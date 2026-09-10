"""Conformance fixtures：合成事件流（无需模型/网络即可测校验器与引擎）。"""

from __future__ import annotations

from typing import Any

from ksadk.harness.events import EventPhase, EventType, RuntimeEvent


def make_event_factory(*, agent_id: str = "agent-1", session_id: str = "sess-1"):
    seq = 0

    def make(event_type: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> RuntimeEvent:
        nonlocal seq
        seq += 1
        return RuntimeEvent.create(
            event_type,
            agent_id=kwargs.pop("agent_id", agent_id),
            user_id=kwargs.pop("user_id", "user-1"),
            session_id=kwargs.pop("session_id", session_id),
            invocation_id=kwargs.pop("invocation_id", "run-1"),
            seq_id=seq,
            payload=payload or {},
            **kwargs,
        )

    return make


def compliant_no_tool_events(make) -> list[RuntimeEvent]:
    """合法流：无工具对话。"""
    return [
        make(EventType.RUN_STARTED, {"status": "in_progress"}),
        make(EventType.TEXT_DELTA, {"text": "你好"}, phase=EventPhase.FINAL_ANSWER.value),
        make(EventType.TEXT_COMPLETED, {"text": "你好"}, phase=EventPhase.FINAL_ANSWER.value),
        make(EventType.RUN_COMPLETED, {"status": "completed"}),
    ]


def compliant_single_tool_events(make) -> list[RuntimeEvent]:
    """合法流：单工具调用（成对）。"""
    return [
        make(EventType.RUN_STARTED, {"status": "in_progress"}),
        make(EventType.MODEL_CALL_STARTED, {"model": "kimi-k3"}),
        make(EventType.MODEL_CALL_COMPLETED, {"model": "kimi-k3"}),
        make(EventType.TOOL_CALL_BEGIN, {"call_id": "tc-1", "name": "budget_lookup", "args": {}}),
        make(EventType.TOOL_CALL_END, {"call_id": "tc-1", "name": "budget_lookup", "result": "42"}),
        make(EventType.TEXT_COMPLETED, {"text": "预算 42"}, phase=EventPhase.FINAL_ANSWER.value),
        make(EventType.RUN_COMPLETED, {"status": "completed"}),
    ]


def broken_unpaired_tool_events(make) -> list[RuntimeEvent]:
    """非法流：tool.call.begin 无 end。"""
    return [
        make(EventType.RUN_STARTED, {"status": "in_progress"}),
        make(EventType.TOOL_CALL_BEGIN, {"call_id": "tc-1", "name": "x", "args": {}}),
        make(EventType.RUN_COMPLETED, {"status": "completed"}),
    ]


def broken_missing_start_events(make) -> list[RuntimeEvent]:
    """非法流：缺 run.started。"""
    return [
        make(EventType.TEXT_COMPLETED, {"text": "x"}, phase=EventPhase.FINAL_ANSWER.value),
        make(EventType.RUN_COMPLETED, {"status": "completed"}),
    ]


def broken_secret_leak_events(make) -> list[RuntimeEvent]:
    """非法流：payload 泄漏 api_key。"""
    return [
        make(EventType.RUN_STARTED, {"status": "in_progress"}),
        make(
            EventType.TOOL_CALL_END,
            {
                "call_id": "tc-1",
                "name": "x",
                "result": {"api_key": "sk-live-123"},
            },
        ),
        make(EventType.RUN_COMPLETED, {"status": "completed"}),
    ]


def canceled_events(make) -> list[RuntimeEvent]:
    """合法流：真实 Cancel 语义。"""
    return [
        make(EventType.RUN_STARTED, {"status": "in_progress"}),
        make(EventType.RUN_CANCELED, {"status": "cancelled", "cancel_result": "interrupted"}),
    ]


__all__ = [
    "broken_missing_start_events",
    "broken_secret_leak_events",
    "broken_unpaired_tool_events",
    "canceled_events",
    "compliant_no_tool_events",
    "compliant_single_tool_events",
    "make_event_factory",
]
