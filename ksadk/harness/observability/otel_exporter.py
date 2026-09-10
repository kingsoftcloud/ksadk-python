"""RuntimeEvent → OpenTelemetry Span 适配 + OTLP 导出（plan §13 / 方案 P0）。

在 RuntimeEvent 事件流之上加 OTel Span 适配层，不改 RuntimeEvent 契约。
按 ``KSADK_OTEL_ENDPOINT`` 环境变量开关；无配置时不导出（零侵入）。

Span 层级映射（plan §13.1 事件层级）::

    Run     → root Span
    Turn    → child Span of Run
    Model   → child Span of Turn
    Tool    → child Span of Turn
    Context → child Span of Turn（compaction/planned）
    Approval→ child Span of Turn
    Memory  → child Span of Turn

成对事件（started/completed）由 Span 自动计时；单次事件（delta/usage）记为
Span 事件（OTel Event，非 Span）。Tool Call begin/end 成对包裹。

配置::

    KSADK_OTEL_ENDPOINT=http://otel-collector:4318/v1/traces
    KSADK_OTEL_SERVICE_NAME=ksadk-harness  (默认)
"""

from __future__ import annotations

import os
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent

#: 事件 → OTel Span 名映射（started 事件创建 Span，completed/failed 结束）。
_SPAN_EVENTS: dict[str, str] = {
    EventType.RUN_STARTED: "harness.run",
    EventType.TURN_STARTED: "harness.turn",
    EventType.MODEL_CALL_STARTED: "harness.model_call",
    EventType.TOOL_CALL_BEGIN: "harness.tool_call",
    EventType.CONTEXT_COMPACTION_STARTED: "harness.compaction",
    EventType.CHECKPOINT_CREATED: "harness.checkpoint",
}

#: started 事件 → 对应的结束事件（completed/failed/end/canceled）。
_SPAN_END_EVENTS: dict[str, set[str]] = {
    EventType.RUN_STARTED: {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELED,
    },
    EventType.TURN_STARTED: {EventType.TURN_COMPLETED},
    EventType.MODEL_CALL_STARTED: {
        EventType.MODEL_CALL_COMPLETED,
        EventType.MODEL_CALL_FAILED,
    },
    EventType.TOOL_CALL_BEGIN: {EventType.TOOL_CALL_END},
    EventType.CONTEXT_COMPACTION_STARTED: {EventType.CONTEXT_COMPACTION_COMPLETED},
}


class OTelSpanAdapter:
    """把 RuntimeEvent 流适配为 OpenTelemetry Span。

    用法：引擎在 stream 事件时调 :meth:`on_event`；适配器内部维护 Span 栈，
    started 事件创建 Span，结束事件结束 Span。

    无 ``KSADK_OTEL_ENDPOINT`` 时为 no-op（不创建 TracerProvider）。
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        service_name: str = "ksadk-harness",
    ) -> None:
        self._endpoint = endpoint or os.getenv("KSADK_OTEL_ENDPOINT", "")
        self._service_name = os.getenv("KSADK_OTEL_SERVICE_NAME", service_name)
        self._tracer: Any | None = None
        self._provider: Any | None = None
        self._spans: dict[str, Any] = {}  # span_key → Span
        self._span_counter: int = 0
        if self._endpoint:
            self._setup_otel()

    def _setup_otel(self) -> None:
        """初始化 OTel TracerProvider + OTLP HTTP exporter。"""
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": self._service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=self._endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        self._provider = provider
        self._tracer = trace.get_tracer("ksadk.harness")

    @property
    def enabled(self) -> bool:
        return self._tracer is not None

    def on_event(self, event: RuntimeEvent) -> None:
        """处理一个 RuntimeEvent——创建/结束对应 OTel Span。"""
        if not self._tracer:
            return

        et = event.event_type

        # started 事件 → 创建 Span
        if et in _SPAN_EVENTS:
            span_name = _SPAN_EVENTS[et]
            span_key = self._span_key(event, et)
            if span_key in self._spans:
                return  # 已存在（重复 started，不重复创建）
            parent_key = self._parent_key(event, et)
            parent_span = self._spans.get(parent_key) if parent_key else None
            context = parent_span.set_span_on_parent() if parent_span else None
            span = self._tracer.start_span(
                span_name,
                context=context,
                attributes=self._attributes(event),
            )
            self._spans[span_key] = span
            return

        # 结束事件 → 结束对应 Span
        for started_et, end_events in _SPAN_END_EVENTS.items():
            if et in end_events:
                span_key = self._span_key(event, started_et)
                span = self._spans.pop(span_key, None)
                if span:
                    if et.endswith(".failed") or et.endswith(".canceled"):
                        span.set_attribute("otel.status_code", "ERROR")
                        span.set_attribute(
                            "error.message", str(event.payload.get("error") or "")
                        )
                    else:
                        span.set_attribute("otel.status_code", "OK")
                    span.end()
                return

        # 单次事件 → 记为 Span Event（非新 Span）
        if self._spans:
            turn_key = self._span_key(event, EventType.TURN_STARTED)
            span = self._spans.get(turn_key) or self._spans.get(
                self._span_key(event, EventType.RUN_STARTED)
            )
            if span:
                span.add_event(
                    et,
                    attributes={
                        k: str(v) for k, v in event.payload.items() if v is not None
                    },
                )

    def flush(self) -> None:
        """强制刷新所有待导出的 Span（进程退出前调）。"""
        if self._provider:
            try:
                self._provider.force_flush()
            except Exception:  # noqa: BLE001 - 导出失败不阻断主流程
                pass

    def shutdown(self) -> None:
        """关闭 TracerProvider。"""
        if self._provider:
            try:
                self._provider.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._provider = None
            self._tracer = None

    # ---- helpers ----

    def _span_key(self, event: RuntimeEvent, started_et: str) -> str:
        """Span 唯一键：run_id + started_event_type + call_id/tool_id。"""
        run_id = event.run_id or event.invocation_id
        extra = ""
        if started_et == EventType.TOOL_CALL_BEGIN:
            extra = f":{event.payload.get('call_id', '')}"
        elif started_et == EventType.MODEL_CALL_STARTED:
            extra = f":{event.payload.get('attempt', '')}"
        return f"{run_id}:{started_et}{extra}"

    def _parent_key(self, event: RuntimeEvent, started_et: str) -> str | None:
        """Span 父键：Turn/Tool 的父是 Run/Turn。"""
        run_id = event.run_id or event.invocation_id
        if started_et == EventType.TURN_STARTED:
            return f"{run_id}:{EventType.RUN_STARTED}"
        if started_et in (
            EventType.MODEL_CALL_STARTED,
            EventType.TOOL_CALL_BEGIN,
            EventType.CONTEXT_COMPACTION_STARTED,
        ):
            return f"{run_id}:{EventType.TURN_STARTED}"
        return None

    @staticmethod
    def _attributes(event: RuntimeEvent) -> dict[str, Any]:
        """从 RuntimeEvent 提取 OTel Span 属性。"""
        attrs: dict[str, Any] = {
            "harness.event_type": event.event_type,
            "harness.agent_id": event.agent_id,
            "harness.session_id": event.session_id,
            "harness.run_id": event.run_id or event.invocation_id,
            "harness.seq_id": event.seq_id,
        }
        if event.phase:
            attrs["harness.phase"] = event.phase
        for key, value in event.payload.items():
            if value is not None and isinstance(value, (str, int, float, bool)):
                attrs[f"harness.payload.{key}"] = value
        return attrs


__all__ = ["OTelSpanAdapter"]
