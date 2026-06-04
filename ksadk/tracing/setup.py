"""
Tracing 初始化 - 支持多 Exporter (InMemory + Langfuse + OTLP)
支持 ADK 自动插桩 via openinference-instrumentation-google-adk
"""

import os
import logging
import atexit
import signal
import base64
from urllib.parse import unquote
from typing import Optional, List, Any

from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter

logger = logging.getLogger(__name__)

_exporter_instance: Optional[InMemoryExporter] = None
_langfuse_exporter: Optional[Any] = None
_tracing_initialized: bool = False
_adk_instrumented: bool = False


def _build_langfuse_otlp_config(langfuse_config: dict = None) -> Optional[dict]:
    """Build Langfuse OTLP direct exporter config from explicit config or env."""
    if langfuse_config:
        public_key = langfuse_config.get("public_key") or ""
        secret_key = langfuse_config.get("secret_key") or ""
        host = langfuse_config.get("host") or "http://localhost:3000"
    else:
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "http://localhost:3000"

    if not public_key or not secret_key:
        return None

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return {
        "endpoint": f"{host.rstrip('/')}/api/public/otel/v1/traces",
        "headers": {
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": "4",
        },
        "protocol": "http/protobuf",
    }


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS into an HTTP headers dict."""
    headers: dict[str, str] = {}
    for part in (raw or "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        headers[key] = unquote(value.strip())
    return headers


def _derive_otlp_traces_endpoint(endpoint: str) -> str:
    """Derive the HTTP trace endpoint from a generic OTLP endpoint."""
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return f"{endpoint}/v1/traces"


def _build_generic_otlp_http_config() -> Optional[dict]:
    """Build generic OTLP HTTP traces exporter config from standard OTEL env."""
    traces_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    endpoint = traces_endpoint or (_derive_otlp_traces_endpoint(base_endpoint) if base_endpoint else "")
    if not endpoint:
        return None

    protocol = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "").strip().lower()
        or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    )
    if protocol and protocol != "http/protobuf":
        logger.warning(
            "Unsupported OTEL_EXPORTER_OTLP protocol for KsADK auto HTTP exporter: %s",
            protocol,
        )
        return None

    raw_headers = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    )
    return {
        "endpoint": endpoint,
        "headers": _parse_otlp_headers(raw_headers),
        "protocol": "http/protobuf",
    }


def setup_tracing(
    enable_inmemory: bool = True,
    enable_langfuse: bool = None,  # Auto-detect from env
    langfuse_config: dict = None,
    enable_otlp: bool = False,
    otlp_endpoint: str = "localhost:4317",
    enable_adk_instrumentation: bool = True,  # Auto-instrument ADK
    use_callback_only: bool = None,  # Explicit override
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
        use_callback_only: 是否仅使用 CallbackHandler (防止 OTel 重复)
    
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
    
    # 2. Generic OTLP HTTP exporter from standard environment variables.
    # This keeps user code backend-agnostic; Langfuse is only one possible OTLP backend.
    generic_otlp_config = _build_generic_otlp_http_config()
    if generic_otlp_config:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            otlp_exporter = OTLPSpanExporter(
                endpoint=generic_otlp_config["endpoint"],
                headers=generic_otlp_config["headers"],
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info(
                "Generic OTLP HTTP exporter enabled: %s (%s)",
                generic_otlp_config["endpoint"],
                generic_otlp_config["protocol"],
            )
        except ImportError as e:
            logger.warning(f"Generic OTLP HTTP exporter not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize generic OTLP HTTP exporter: {e}")

    # 3. Langfuse OTLP direct exporter (auto-detect or explicit config)
    # 注意: 对于 LangGraph/LangChain 框架，推荐使用 CallbackHandler 而非 OTel Exporter
    # 同时使用两者会导致重复的 trace
    langfuse_enabled = enable_langfuse
    if langfuse_enabled is None:
        # Auto-detect from environment variables
        langfuse_enabled = (
            not generic_otlp_config
            and bool(os.getenv("LANGFUSE_PUBLIC_KEY") or (langfuse_config or {}).get("public_key"))
        )
    
    # 检查是否应该禁用 LangfuseExporter (当使用 LangChain/LangGraph 时)
    # 优先使用显式参数，否则读取环境变量
    if use_callback_only is None:
        use_callback_only = os.getenv("LANGFUSE_USE_CALLBACK", "false").lower() == "true"
    
    if langfuse_enabled and not use_callback_only:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            config = _build_langfuse_otlp_config(langfuse_config)
            if config:
                otlp_exporter = OTLPSpanExporter(
                    endpoint=config["endpoint"],
                    headers=config["headers"],
                )
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info(
                    "Langfuse OTLP exporter enabled: %s (%s)",
                    config["endpoint"],
                    config["protocol"],
                )
            else:
                logger.warning("Langfuse credentials not found, skipping")
                
        except ImportError as e:
            logger.warning(f"Langfuse OTLP exporter not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize Langfuse OTLP exporter: {e}")
    elif langfuse_enabled:
        logger.info("Langfuse will use CallbackHandler (recommended for LangChain/LangGraph)")
    
    # 4. OTLP Exporter (optional)
    if enable_otlp:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info(f"OTLP exporter enabled: {otlp_endpoint}")
        except ImportError:
            logger.warning("OTLP exporter not installed")
    
    # 5. ADK Auto-Instrumentation (for Google ADK projects)
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
            
    # 6. LangChain Auto-Instrumentation
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
