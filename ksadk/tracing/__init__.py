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
    LANGFUSE_PUBLIC_KEY   - 自动启用 Langfuse
    LANGFUSE_SECRET_KEY   - Langfuse Secret
    LANGFUSE_BASE_URL     - Langfuse 服务地址

Agent / session / user 等业务维度建议作为 span attributes 写入。
"""

from ksadk.tracing.setup import setup_tracing, get_memory_exporter, get_tracer

__all__ = ["setup_tracing", "get_memory_exporter", "get_tracer"]
