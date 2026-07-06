"""
KsADK Tracing - 可观测性模块 (OpenTelemetry)

使用方式:
    from ksadk.tracing import setup_tracing

    # 优先使用标准 OTel/OTLP 环境变量，后端可以是 Langfuse 或其他 Collector
    # OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com/otel
    # OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
    setup_tracing()

    # 兼容旧 Langfuse 环境变量，也可以显式启用
    setup_tracing(enable_langfuse=True)

环境变量:
    OTEL_EXPORTER_OTLP_ENDPOINT          - 通用 OTLP endpoint
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT   - traces 专用 OTLP endpoint
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL   - traces 专用 OTLP 协议
    OTEL_EXPORTER_OTLP_TRACES_HEADERS    - traces 专用 OTLP headers
    CLOUD_MONITOR_APP_KEY                - 云监控 OTLP AppKey,注入 Ksc-Appkey header
    CLOUD_MONITOR_OTLP_ENABLED           - 云监控 OTLP 导出开关 (0.6.7 新增)
    CLOUD_MONITOR_OTLP_ENDPOINT          - 云监控 OTLP endpoint
    CLOUD_MONITOR_OTLP_PROTOCOL          - 云监控 OTLP 协议
    CLOUD_MONITOR_OTLP_HEADERS           - 云监控 OTLP headers (0.6.7 新增)
    CLOUD_MONITOR_OTLP_TRACES_ENDPOINT   - 云监控 traces 专用 endpoint
    CLOUD_MONITOR_OTLP_TRACES_PROTOCOL   - 云监控 traces 专用协议
    LANGFUSE_PUBLIC_KEY   - 存在即自动启用 Langfuse (无独立 LANGFUSE_ENABLED 开关)
    LANGFUSE_SECRET_KEY   - Langfuse Secret
    LANGFUSE_BASE_URL     - Langfuse 服务地址 (优先)
    LANGFUSE_HOST         - Langfuse 服务地址 (LANGFUSE_BASE_URL 别名)

Agent / session / user 等业务维度建议作为 span attributes 写入。
"""

from ksadk.tracing.setup import get_memory_exporter, get_tracer, setup_tracing

__all__ = ["setup_tracing", "get_memory_exporter", "get_tracer"]
