"""性能指标采集器单测（plan §20.4）。

用合成事件流验证 9 个指标计算，不依赖真实模型。
"""

from __future__ import annotations

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.observability.metrics import collect_metrics


def _ev(
    event_type: str,
    ts: float,
    *,
    run_id: str = "run-1",
    seq: int = 1,
    payload: dict | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"e-{seq}",
        event_type=event_type,
        timestamp=ts,
        agent_id="a",
        user_id="u",
        session_id="s",
        invocation_id=run_id,
        seq_id=seq,
        payload=payload or {},
        run_id=run_id,
    )


def test_empty_events_returns_empty_metrics():
    m = collect_metrics([])
    assert m.total_events == 0
    assert m.first_token_latency_ms == -1.0


def test_first_token_latency():
    """run.started → 首个 text.delta 的时间差。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.TEXT_DELTA, 1.5, seq=2, payload={"text": "H"}),
        _ev(EventType.TEXT_DELTA, 1.6, seq=3, payload={"text": "i"}),
        _ev(EventType.TEXT_COMPLETED, 1.7, seq=4, payload={"text": "Hi"}),
        _ev(EventType.RUN_COMPLETED, 1.8, seq=5),
    ]
    m = collect_metrics(events)
    assert m.first_token_latency_ms == 500.0  # 1.5 - 1.0 = 0.5s = 500ms


def test_context_build_latency():
    """context.planned - turn.started。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.TURN_STARTED, 1.1, seq=2),
        _ev(EventType.CONTEXT_PLANNED, 1.15, seq=3),
    ]
    m = collect_metrics(events)
    assert m.context_build_latency_ms == 50.0  # 1.15 - 1.1 = 0.05s


def test_tool_overhead():
    """tool.call.begin - 前一 model.call.completed。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.TURN_STARTED, 1.1, seq=2),
        _ev(EventType.MODEL_CALL_STARTED, 1.2, seq=3),
        _ev(EventType.MODEL_CALL_COMPLETED, 1.5, seq=4),
        _ev(EventType.TOOL_CALL_BEGIN, 1.52, seq=5, payload={"call_id": "tc1"}),
        _ev(EventType.TOOL_CALL_END, 1.8, seq=6),
    ]
    m = collect_metrics(events)
    assert m.tool_overhead_ms == 20.0  # 1.52 - 1.5 = 0.02s


def test_checkpoint_write_latency():
    """checkpoint.created 事件耗时字段。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(
            EventType.CHECKPOINT_CREATED, 1.5, seq=2,
            payload={"duration_ms": 12.5},
        ),
    ]
    m = collect_metrics(events)
    assert m.checkpoint_write_latency_ms == 12.5


def test_resume_latency():
    """run.resumed - resume 请求接收时间。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(
            EventType.RUN_RESUMED, 3.0, seq=2,
            payload={"requested_at": 2.5},
        ),
    ]
    m = collect_metrics(events)
    assert m.resume_latency_ms == 500.0  # 3.0 - 2.5 = 0.5s


def test_compaction_count_and_tokens():
    """context.compaction.* 事件计数 + usage.reported 压缩 Token。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.CONTEXT_COMPACTION_STARTED, 2.0, seq=2),
        _ev(EventType.CONTEXT_COMPACTION_COMPLETED, 2.5, seq=3),
        _ev(
            EventType.USAGE_REPORTED, 3.0, seq=4,
            payload={"input_tokens": 100, "compaction_tokens": 40},
        ),
    ]
    m = collect_metrics(events)
    assert m.compaction_count == 2  # started + completed
    assert m.compaction_tokens == 40


def test_prompt_cache_hit_rate():
    """cached_tokens / input_tokens。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(
            EventType.USAGE_REPORTED, 2.0, seq=2,
            payload={"input_tokens": 1000, "cached_tokens": 800},
        ),
        _ev(
            EventType.USAGE_REPORTED, 3.0, seq=3,
            payload={"input_tokens": 500, "cached_tokens": 300},
        ),
    ]
    m = collect_metrics(events)
    # (800 + 300) / (1000 + 500) = 1100/1500 ≈ 0.7333
    assert m.prompt_cache_hit_rate == round(1100 / 1500, 4)


def test_mcp_recovery_time():
    """capability 降级→恢复时间差。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.CAPABILITY_DEGRADED, 2.0, seq=2),
        _ev(EventType.CAPABILITY_RECOVERED, 2.5, seq=3),
    ]
    m = collect_metrics(events)
    assert m.mcp_recovery_ms == 500.0  # 2.5 - 2.0


def test_missing_data_returns_negative_one():
    """缺数据的指标标记 -1（不伪造零值）。"""
    events = [
        _ev(EventType.RUN_STARTED, 1.0, seq=1),
        _ev(EventType.RUN_COMPLETED, 2.0, seq=2),
    ]
    m = collect_metrics(events)
    assert m.first_token_latency_ms == -1.0
    assert m.context_build_latency_ms == -1.0
    assert m.tool_overhead_ms == -1.0
