"""Harness 可观测性适配层（OTel/OTLP 导出 + Context 检查）。

不改变 RuntimeEvent 契约——在事件流之上加 OpenTelemetry Span 适配，
按 ``KSADK_OTEL_ENDPOINT`` 环境变量开关。无配置时不导出（零侵入）。

原 ``observability.py`` 的 context_trace / compaction_trace / token_report /
context_inspection / capability_health 保留在 :mod:`inspection` 子模块。
"""

from ksadk.harness.observability.inspection import (
    capability_health,
    compaction_trace,
    context_inspection,
    context_trace,
    token_report,
)
from ksadk.harness.observability.metrics import PerformanceMetrics, collect_metrics
from ksadk.harness.observability.otel_exporter import OTelSpanAdapter

__all__ = [
    "OTelSpanAdapter",
    "PerformanceMetrics",
    "capability_health",
    "collect_metrics",
    "compaction_trace",
    "context_inspection",
    "context_trace",
    "token_report",
]
