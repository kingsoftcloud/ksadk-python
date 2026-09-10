"""性能指标采集器（plan §20.4）。

从 RuntimeEvent 事件流的时间戳与 payload 派生 9 个性能指标。
纯计算，不依赖真实模型调用——任何事件流（含 fixture）均可采集。

指标（plan §20.4）：
  - context_build_latency_ms: context.planned 与 turn.started 的时间差
  - first_token_latency_ms: run.started 到首个 text.delta 的时间差
  - tool_overhead_ms: tool.call.begin 与前一 model.call.completed 的时间差
  - checkpoint_write_latency_ms: checkpoint.created 事件自身耗时字段
  - resume_latency_ms: run.resumed 与 resume 请求接收的时间差
  - compaction_count: context.compaction.* 事件计数
  - compaction_tokens: usage.reported 中的压缩 Token
  - prompt_cache_hit_rate: cached_input_tokens / input_tokens
  - mcp_recovery_ms: capability 降级与恢复事件的时间差
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent


@dataclass
class PerformanceMetrics:
    """从一次 Run 的事件流派生的性能指标。"""

    # 延迟类（毫秒，-1 表示数据不足）
    context_build_latency_ms: float = -1.0
    first_token_latency_ms: float = -1.0
    tool_overhead_ms: float = -1.0
    checkpoint_write_latency_ms: float = -1.0
    resume_latency_ms: float = -1.0
    mcp_recovery_ms: float = -1.0

    # 计数/比率类
    compaction_count: int = 0
    compaction_tokens: int = 0
    prompt_cache_hit_rate: float = 0.0

    # 原始数据（供审计）
    run_id: str = ""
    total_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "context_build_latency_ms": self.context_build_latency_ms,
            "first_token_latency_ms": self.first_token_latency_ms,
            "tool_overhead_ms": self.tool_overhead_ms,
            "checkpoint_write_latency_ms": self.checkpoint_write_latency_ms,
            "resume_latency_ms": self.resume_latency_ms,
            "mcp_recovery_ms": self.mcp_recovery_ms,
            "compaction_count": self.compaction_count,
            "compaction_tokens": self.compaction_tokens,
            "prompt_cache_hit_rate": self.prompt_cache_hit_rate,
            "total_events": self.total_events,
        }


def collect_metrics(events: list[RuntimeEvent]) -> PerformanceMetrics:
    """从事件流派生性能指标（plan §20.4）。

    所有指标从事件 timestamp + payload 派生，不做额外埋点。
    数据不足的指标标记 -1（不伪造零值）。
    """
    metrics = PerformanceMetrics(total_events=len(events))
    if not events:
        return metrics

    metrics.run_id = events[0].run_id or events[0].invocation_id

    # 按事件类型分组索引（保留首末出现）
    first_by_type: dict[str, RuntimeEvent] = {}
    for event in events:
        et = event.event_type
        if et not in first_by_type:
            first_by_type[et] = event

    # 1. context_build_latency: context.planned - turn.started
    if EventType.TURN_STARTED in first_by_type and EventType.CONTEXT_PLANNED in first_by_type:
        turn_start = first_by_type[EventType.TURN_STARTED]
        ctx_planned = first_by_type[EventType.CONTEXT_PLANNED]
        metrics.context_build_latency_ms = _delta_ms(turn_start, ctx_planned)

    # 2. first_token_latency: run.started → 首个 text.delta
    if EventType.RUN_STARTED in first_by_type:
        run_start = first_by_type[EventType.RUN_STARTED]
        first_delta = next(
            (e for e in events if e.event_type == EventType.TEXT_DELTA), None
        )
        if first_delta:
            metrics.first_token_latency_ms = _delta_ms(run_start, first_delta)

    # 3. tool_overhead: tool.call.begin - 前一 model.call.completed
    tool_begins = [e for e in events if e.event_type == EventType.TOOL_CALL_BEGIN]
    if tool_begins:
        latencies: list[float] = []
        for tb in tool_begins:
            # 找 tb 之前最近的 model.call.completed
            prev_model_completed = None
            for e in events:
                if e.timestamp > tb.timestamp:
                    break
                if e.event_type == EventType.MODEL_CALL_COMPLETED:
                    prev_model_completed = e
            if prev_model_completed:
                latencies.append(_delta_ms(prev_model_completed, tb))
        if latencies:
            metrics.tool_overhead_ms = sum(latencies) / len(latencies)

    # 4. checkpoint_write_latency: checkpoint.created 事件耗时字段
    cp_event = first_by_type.get(EventType.CHECKPOINT_CREATED)
    if cp_event:
        metrics.checkpoint_write_latency_ms = float(
            cp_event.payload.get("duration_ms", -1.0)
        )

    # 5. resume_latency: run.resumed - resume 请求接收
    resumed_event = first_by_type.get(EventType.RUN_RESUMED)
    if resumed_event:
        resume_requested_at = resumed_event.payload.get("requested_at")
        if resume_requested_at:
            metrics.resume_latency_ms = max(
                0.0, (resumed_event.timestamp - float(resume_requested_at)) * 1000
            )

    # 6. compaction_count + tokens
    compaction_events = [
        e for e in events if e.event_type.startswith("context.compaction")
    ]
    metrics.compaction_count = len(compaction_events)
    for e in events:
        if e.event_type == EventType.USAGE_REPORTED:
            metrics.compaction_tokens += int(e.payload.get("compaction_tokens", 0))

    # 7. prompt_cache_hit_rate: cached_tokens / input_tokens
    usage_events = [e for e in events if e.event_type == EventType.USAGE_REPORTED]
    if usage_events:
        total_input = sum(int(e.payload.get("input_tokens", 0)) for e in usage_events)
        total_cached = sum(int(e.payload.get("cached_tokens", 0)) for e in usage_events)
        if total_input > 0:
            metrics.prompt_cache_hit_rate = round(total_cached / total_input, 4)

    # 8. mcp_recovery: capability 降级→恢复时间差
    degraded_at: float | None = None
    for e in events:
        if e.event_type == EventType.CAPABILITY_DEGRADED:
            degraded_at = e.timestamp
        elif (
            degraded_at is not None
            and e.event_type == EventType.CAPABILITY_RECOVERED
        ):
            metrics.mcp_recovery_ms = (e.timestamp - degraded_at) * 1000
            degraded_at = None

    return metrics


def _delta_ms(earlier: RuntimeEvent, later: RuntimeEvent) -> float:
    """两事件时间差（毫秒）。"""
    return round((later.timestamp - earlier.timestamp) * 1000, 2)


__all__ = ["PerformanceMetrics", "collect_metrics"]
