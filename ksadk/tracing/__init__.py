"""
KsADK Tracing - 可观测性模块 (OpenTelemetry)

使用方式:
    from ksadk.tracing import setup_tracing

    # 使用标准 OTel/OTLP 环境变量，直接连接任意 OTLP 兼容后端
    # OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com/otel
    # OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
    setup_tracing()

环境变量:
    OTEL_EXPORTER_OTLP_ENDPOINT          - 通用 OTLP endpoint (托管 Agent 的 Langfuse 主路)
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT    - traces 专用 OTLP endpoint
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL    - traces 专用 OTLP 协议
    OTEL_EXPORTER_OTLP_TRACES_HEADERS     - traces 专用 OTLP headers
    CLOUD_MONITOR_OTLP_ENDPOINT            - 云监控 OTLP endpoint (次路)
    CLOUD_MONITOR_OTLP_TRACES_ENDPOINT     - 云监控 traces 专用 endpoint
    CLOUD_MONITOR_OTLP_HEADERS             - 云监控 OTLP headers (含 Ksc-Appkey)
    CLOUD_MONITOR_OTLP_TRACES_HEADERS      - 云监控 traces 专用 headers，优先于通用 headers
    CLOUD_MONITOR_OTLP_PROTOCOL            - 云监控 OTLP 协议
    CLOUD_MONITOR_OTLP_TRACES_PROTOCOL      - 云监控 traces 专用协议
    CLOUD_MONITOR_APP_KEY                  - 已废弃:仅当两个 headers 变量整体缺失时回退

Agent / session / user 等业务维度建议作为 span attributes 写入。
"""

from ksadk.tracing.setup import (
    get_memory_exporter,
    get_tracer,
    setup_tracing,
    shutdown_tracing,
)

__all__ = ["setup_tracing", "shutdown_tracing", "get_memory_exporter", "get_tracer"]
