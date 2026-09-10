"""Memory 写回闭环（缺口 3 / plan §9.3 写入管线的运行时接线）。

Run 结束后从对话消息做**确定性提取**（``propose_memory_candidates``，无
LLM），逐条走 :class:`HarnessMemoryRuntime.write` 的受控写入管线——
来源校验、scope 越权、敏感标签、去重/冲突（Coordinator propose_and_commit）
全部生效；每条尝试产出一条审计事件（``memory.write`` / ``memory.conflict``）。

审计事件**不并入** RuntimeEvent 流（Conformance 要求终止事件必须是最后
一条），由宿主经 ``audit_sink`` 落事件存储，Studio 在 Run 详情里可见。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Iterable

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.memory_runtime import (
    HarnessMemoryError,
    HarnessMemoryRuntime,
    MemoryWriteRequest,
)
from ksadk.harness.spec import HarnessSpec
from ksadk.memory.extraction import propose_memory_candidates
from ksadk.memory.models import MemoryCandidate

#: 提取候选的单 Run 上限——确定性提取一次对话产出有限，防御异常膨胀。
_MAX_CANDIDATES_PER_RUN = 16


def _message_to_event(message: Any, index: int) -> SimpleNamespace | None:
    """把 HarnessState.Message 转成提取器认识的事件形态。"""
    role = str(getattr(message.role, "value", "") or message.role)
    text = str(getattr(message, "content", "") or "").strip()
    if not text:
        return None
    if role == "user":
        event_type, author = "user_message", "user"
    elif role == "assistant":
        event_type, author = "assistant_message", "assistant"
    elif role == "tool":
        event_type, author = "tool_result", "assistant"
    else:
        return None
    return SimpleNamespace(
        id=f"msg_{index}",
        event_type=event_type,
        author=author,
        seq_id=index + 1,
        text=text,
        content={"text": text},
        metadata={},
    )


def _pick_scope(spec: HarnessSpec) -> tuple[str, str] | None:
    """按 memory_policy scopes 选写回作用域（优先 agent，其次 user）。"""
    if not spec.memory_policy.enabled:
        return None
    scopes = set(spec.memory_policy.scopes)
    for scope in ("agent", "user"):
        if scope in scopes:
            return scope, scope
    return None


def write_back_messages(
    memory_runtime: HarnessMemoryRuntime,
    spec: HarnessSpec,
    *,
    run_id: str,
    agent_id: str,
    user_id: str,
    messages: Iterable[Any],
    audit_sink: Callable[[RuntimeEvent], None] | None = None,
) -> list[RuntimeEvent]:
    """Run 结束后的受控写回：提取 → 去重/冲突 → 受控写入 → 审计。

    返回审计事件列表（同时经 ``audit_sink`` 逐条外送）。任何单条失败都
    只影响该条（rejected 审计），绝不抛出——写回是后置链路，不能影响
    已完成的 Run。
    """
    picked = _pick_scope(spec)
    if picked is None:
        return []
    scope, _ = picked
    scope_id = f"agent:{agent_id}" if scope == "agent" else f"user:{user_id or 'anonymous'}"

    events = [ev for i, m in enumerate(messages) if (ev := _message_to_event(m, i)) is not None]
    if not events:
        return []
    candidates: list[MemoryCandidate] = propose_memory_candidates(
        events,
        scope=scope,
        scope_id=scope_id,  # type: ignore[arg-type]
    )[:_MAX_CANDIDATES_PER_RUN]
    if not candidates:
        return []

    audit_events: list[RuntimeEvent] = []
    seq = 0
    for candidate in candidates:
        request = MemoryWriteRequest(
            operation=candidate.operation,
            content=candidate.content,
            scope=candidate.scope,  # type: ignore[arg-type]
            scope_id=candidate.scope_id,
            source=candidate.reason or "extraction",
            source_event_id=(candidate.source_event_ids or [""])[0],
            confidence=float(candidate.confidence),
            importance=float(candidate.importance),
            reason=candidate.reason,
            memory_type=candidate.memory_type,
            slot_key=candidate.slot_key,
        )
        event = _write_one(memory_runtime, request, spec, run_id=run_id, seq=seq)
        if event is None:
            continue
        seq = event.seq_id
        audit_events.append(event)
        if audit_sink is not None:
            audit_sink(event)
    return audit_events


def _write_one(
    memory_runtime: HarnessMemoryRuntime,
    request: MemoryWriteRequest,
    spec: HarnessSpec,
    *,
    run_id: str,
    seq: int,
) -> RuntimeEvent | None:
    """单条受控写入；违规（HarnessMemoryError）落 rejected 审计而非抛出。"""
    try:
        _, event = memory_runtime.write(request, spec, run_id=run_id)
        return event
    except HarnessMemoryError as exc:
        return RuntimeEvent.create(
            EventType.MEMORY_WRITE,
            agent_id="",
            user_id="",
            session_id="",
            invocation_id=run_id,
            seq_id=seq + 1,
            payload={
                "scope": str(request.scope),
                "memory_ref": request.scope_id,
                "decision": "rejected",
                "source": request.source,
                "reason": str(exc),
            },
        )


__all__ = ["write_back_messages"]
