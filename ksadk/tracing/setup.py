"""
Tracing 初始化 - 支持多 Exporter (InMemory + Langfuse + OTLP)
支持 ADK 自动插桩 via openinference-instrumentation-google-adk
"""

import os
import logging
import atexit
import signal
from typing import Optional, List, Any

from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter

logger = logging.getLogger(__name__)

_exporter_instance: Optional[InMemoryExporter] = None
_langfuse_exporter: Optional[Any] = None
_tracing_initialized: bool = False
_adk_instrumented: bool = False


def setup_tracing(
    enable_inmemory: bool = True,
    enable_langfuse: bool = None,  # Auto-detect from env
    langfuse_config: dict = None,
    enable_otlp: bool = False,
    otlp_endpoint: str = "localhost:4317",
    enable_adk_instrumentation: bool = True,  # Auto-instrument ADK
    **kwargs
) -> Optional[InMemoryExporter]:
    """初始化 Tracing (支持多 Exporter)
    
    Args:
        enable_inmemory: 是否启用内存 Exporter (Web UI 使用)
        enable_langfuse: 是否启用 Langfuse (None = 自动检测环境变量)
        langfuse_config: Langfuse 配置 {"public_key", "secret_key", "host"}
        enable_otlp: 是否启用 OTLP Exporter
        otlp_endpoint: OTLP 端点地址
        enable_adk_instrumentation: 是否启用 ADK 自动插桩
    
    Returns:
        InMemoryExporter 实例 (用于 Web UI 获取 traces)
    """
    global _exporter_instance, _langfuse_exporter, _tracing_initialized, _adk_instrumented
    
    # 防止重复初始化
    if _tracing_initialized:
        logger.debug("Tracing already initialized, skipping")
        return _exporter_instance
    
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry not installed, tracing disabled")
        return None
    
    # 检查是否已有 TracerProvider (避免覆盖)
    existing_provider = trace.get_tracer_provider()
    if existing_provider and hasattr(existing_provider, 'add_span_processor'):
        # 使用现有 provider，直接添加 processor
        provider = existing_provider
        logger.debug("Using existing TracerProvider")
    else:
        # 创建新的 TracerProvider
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        logger.debug("Created new TracerProvider")
    
    # 1. InMemory Exporter (for Web UI)
    if enable_inmemory:
        exporter = InMemoryExporter(max_traces=kwargs.get("max_traces", 1000))
        _exporter_instance = exporter
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        logger.info("InMemory exporter enabled")
    
    # 2. Langfuse Exporter (auto-detect or explicit config)
    langfuse_enabled = enable_langfuse
    if langfuse_enabled is None:
        # Auto-detect from environment variables
        langfuse_enabled = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
    
    if langfuse_enabled:
        try:
            from ksadk.tracing.exporters.langfuse_exporter import LangfuseExporter, LangfuseConfig
            
            # Get config from parameter or environment
            if langfuse_config:
                config = LangfuseConfig(
                    public_key=langfuse_config.get("public_key"),
                    secret_key=langfuse_config.get("secret_key"),
                    host=langfuse_config.get("host", "http://localhost:3000")
                )
            else:
                # Read from environment variables
                config = LangfuseConfig(
                    public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
                    secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
                    host=os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000")
                )
            
            if config.public_key and config.secret_key:
                langfuse_exp = LangfuseExporter(config)
                _langfuse_exporter = langfuse_exp
                if langfuse_exp.processor:
                    provider.add_span_processor(langfuse_exp.processor)
                    logger.info(f"Langfuse exporter enabled: {config.host}")
            else:
                logger.warning("Langfuse credentials not found, skipping")
                
        except ImportError as e:
            logger.warning(f"Langfuse not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize Langfuse: {e}")
    
    # 3. OTLP Exporter (optional)
    if enable_otlp:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info(f"OTLP exporter enabled: {otlp_endpoint}")
        except ImportError:
            logger.warning("OTLP exporter not installed")
    
    # 4. ADK Auto-Instrumentation (for Google ADK projects)
    if enable_adk_instrumentation and not _adk_instrumented:
        try:
            from openinference.instrumentation.google_adk import GoogleADKInstrumentor
            GoogleADKInstrumentor().instrument()
            _adk_instrumented = True
            logger.info("Google ADK instrumentation enabled")
        except ImportError:
            logger.debug("openinference-instrumentation-google-adk not installed, ADK auto-instrumentation disabled")
        except Exception as e:
            logger.debug(f"ADK instrumentation failed: {e}")
            
    # 5. LangChain Auto-Instrumentation
    if enable_adk_instrumentation:
        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor
            LangChainInstrumentor().instrument()
            logger.info("LangChain instrumentation enabled")
        except ImportError:
            logger.warning("Auto-instrumentation skipped: 'openinference-instrumentation-langchain' not installed. Install with `pip install ksadk[tracing]` for detailed traces.")
        except Exception as e:
            logger.error(f"LangChain instrumentation failed: {e}")
    
    # Register graceful shutdown
    atexit.register(shutdown_tracing)
    
    _tracing_initialized = True
    return _exporter_instance


def shutdown_tracing():
    """Gracefully shutdown tracing to prevent Ctrl+C errors"""
    global _langfuse_exporter, _tracing_initialized
    
    if _langfuse_exporter is not None:
        try:
            if hasattr(_langfuse_exporter, '_exporter'):
                _langfuse_exporter._exporter.shutdown()
            logger.debug("Langfuse exporter shutdown gracefully")
        except Exception:
            pass  # Ignore shutdown errors
    
    _tracing_initialized = False


def get_memory_exporter() -> Optional[InMemoryExporter]:
    """获取当前的 Memory Exporter 实例"""
    return _exporter_instance


def get_langfuse_exporter():
    """获取 Langfuse Exporter 实例"""
    return _langfuse_exporter


def get_tracer(name: str = "ksadk"):
    """获取 Tracer"""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return None

