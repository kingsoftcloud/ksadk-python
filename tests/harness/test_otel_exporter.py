"""OTelSpanAdapter 单测（mock TracerProvider，不发真实 OTLP）。

验证：RuntimeEvent → OTel Span 映射正确，started/completed 成对，
单次事件记为 Span Event，无 endpoint 时 no-op。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.observability.otel_exporter import OTelSpanAdapter


def _make_event(
    event_type: str,
    *,
    agent_id: str = "agent-1",
    session_id: str = "sess-1",
    run_id: str = "run-1",
    seq_id: int = 1,
    payload: dict | None = None,
    phase: str | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt-{seq_id}",
        event_type=event_type,
        timestamp=float(seq_id),
        agent_id=agent_id,
        user_id="user-1",
        session_id=session_id,
        invocation_id=run_id,
        seq_id=seq_id,
        phase=phase,
        payload=payload or {},
        run_id=run_id,
    )


def test_no_endpoint_is_noop():
    """无 KSADK_OTEL_ENDPOINT 时 adapter 不导出。"""
    with patch.dict("os.environ", {}, clear=True):
        adapter = OTelSpanAdapter()
    assert not adapter.enabled


def test_with_endpoint_sets_up_tracer():
    """有 endpoint 时初始化 TracerProvider。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    assert adapter.enabled
    adapter.shutdown()


def test_run_started_creates_root_span():
    """run.started 创建 root Span。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    # 用 mock 替换 tracer，不发真实 OTLP
    mock_span = MagicMock()
    adapter._tracer = MagicMock()
    adapter._tracer.start_span.return_value = mock_span

    adapter.on_event(
        _make_event(EventType.RUN_STARTED, seq_id=1, payload={"status": "in_progress"})
    )

    adapter._tracer.start_span.assert_called_once()
    assert "harness.run" in str(adapter._tracer.start_span.call_args)
    assert mock_span in adapter._spans.values()
    adapter.shutdown()


def test_run_completed_ends_span():
    """run.completed 结束对应 Span。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    mock_span = MagicMock()
    adapter._tracer = MagicMock()
    adapter._tracer.start_span.return_value = mock_span

    adapter.on_event(_make_event(EventType.RUN_STARTED, seq_id=1))
    adapter.on_event(
        _make_event(EventType.RUN_COMPLETED, seq_id=2, payload={"status": "completed"})
    )

    mock_span.end.assert_called_once()
    mock_span.set_attribute.assert_any_call("otel.status_code", "OK")
    assert len(adapter._spans) == 0  # 已弹出
    adapter.shutdown()


def test_run_failed_sets_error_status():
    """run.failed 设置 ERROR 状态。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    mock_span = MagicMock()
    adapter._tracer = MagicMock()
    adapter._tracer.start_span.return_value = mock_span

    adapter.on_event(_make_event(EventType.RUN_STARTED, seq_id=1))
    adapter.on_event(
        _make_event(EventType.RUN_FAILED, seq_id=2, payload={"error": "timeout"})
    )

    mock_span.set_attribute.assert_any_call("otel.status_code", "ERROR")
    mock_span.set_attribute.assert_any_call("error.message", "timeout")
    mock_span.end.assert_called_once()
    adapter.shutdown()


def test_tool_call_pair_creates_and_ends_span():
    """tool.call.begin/end 成对创建/结束 Span。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    mock_span = MagicMock()
    adapter._tracer = MagicMock()
    adapter._tracer.start_span.return_value = mock_span

    adapter.on_event(
        _make_event(
            EventType.TOOL_CALL_BEGIN,
            seq_id=3,
            payload={"call_id": "tc-1", "name": "lookup", "args": {}},
        )
    )
    assert mock_span in adapter._spans.values()

    adapter.on_event(
        _make_event(
            EventType.TOOL_CALL_END,
            seq_id=4,
            payload={"call_id": "tc-1", "name": "lookup", "result": "ok"},
        )
    )
    mock_span.end.assert_called_once()
    assert len(adapter._spans) == 0
    adapter.shutdown()


def test_single_event_records_as_span_event():
    """单次事件（text.delta/usage.reported）记为 Span Event，非新 Span。"""
    adapter = OTelSpanAdapter(endpoint="http://localhost:4318/v1/traces")
    root_span = MagicMock()
    adapter._tracer = MagicMock()
    adapter._tracer.start_span.return_value = root_span

    adapter.on_event(_make_event(EventType.RUN_STARTED, seq_id=1))
    # text.delta 是单次事件 → 记为 root Span 的 Span Event
    adapter.on_event(
        _make_event(EventType.TEXT_DELTA, seq_id=2, payload={"text": "Hello"})
    )
    root_span.add_event.assert_called_once()
    assert root_span.add_event.call_args[0][0] == EventType.TEXT_DELTA
    adapter.shutdown()


def test_flush_and_shutdown():
    """flush/shutdown 不抛错（无 provider 时也安全）。"""
    with patch.dict("os.environ", {}, clear=True):
        adapter = OTelSpanAdapter()
    adapter.flush()  # no-op
    adapter.shutdown()  # no-op
