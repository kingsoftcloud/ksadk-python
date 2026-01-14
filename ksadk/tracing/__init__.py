"""
KsADK Tracing - 可观测性模块 (OpenTelemetry)

使用方式:
    from ksadk.tracing import setup_tracing
    
    # 自动检测 Langfuse (如果设置了 LANGFUSE_PUBLIC_KEY)
    setup_tracing()
    
    # 或显式配置
    setup_tracing(enable_langfuse=True)

环境变量:
    LANGFUSE_PUBLIC_KEY   - 自动启用 Langfuse
    LANGFUSE_SECRET_KEY   - Langfuse Secret
    LANGFUSE_BASE_URL     - Langfuse 服务地址

Agent 信息通过 ksadk.configs.settings.agent 配置。
"""

from ksadk.tracing.setup import setup_tracing, get_memory_exporter, get_tracer

__all__ = ["setup_tracing", "get_memory_exporter", "get_tracer"]
