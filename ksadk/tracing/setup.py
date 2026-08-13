"""
Tracing 初始化 - 标准 OTLP 双写（Langfuse 主 + CloudMonitor 次）

每个 Python Agent 进程一个 TracerProvider，挂两个 OTLP/HTTP protobuf BatchSpanProcessor，
消费同一批 ReadableSpan。不在 exporter 阶段按目的地改写 span。

支持 ADK 自动插桩 via openinference-instrumentation-google-adk
"""

import atexit
import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter

logger = logging.getLogger(__name__)


def _batch_span_processor_kwargs() -> dict:
    """BatchSpanProcessor 参数：限制单次 export 体积，避免 OTLP collector 413。

    默认 max_export_batch_size=64（OTel 默认 512），单 batch 过大会触发 collector
    `Request Entity Too Large (413)`。可用 KSADK_OTLP_MAX_EXPORT_BATCH_SIZE 覆盖。
    """
    try:
        max_batch = int(os.environ.get("KSADK_OTLP_MAX_EXPORT_BATCH_SIZE", "64"))
    except ValueError:
        max_batch = 64
    if max_batch <= 0:
        max_batch = 64
    return {
        "max_queue_size": max(512, max_batch * 8),
        "max_export_batch_size": max_batch,
        "export_timeout_millis": 30000,
    }


_exporter_instance: Optional[InMemoryExporter] = None
_tracing_initialized: bool = False
_adk_instrumented: bool = False
_managed_span_processors: list[Any] = []


@dataclass(frozen=True)
class _OtlpHttpConfig:
    endpoint: str
    headers: dict[str, str]
    protocol: str
    service_name: str


class _LoggingSpanExporter(SpanExporter):
    """Delegating exporter that logs external trace export flow."""

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        name: str,
        endpoint: str,
        service_name: str,
        header_keys: list[str],
    ):
        self._exporter = exporter
        self._name = name
        self._endpoint = endpoint
        self._service_name = service_name
        self._header_keys = header_keys

    def export(self, spans) -> SpanExportResult:
        span_count = len(spans)
        logger.info(
            "%s export started: endpoint=%s service_name=%s spans=%s headers=%s",
            self._name,
            self._endpoint,
            self._service_name or "",
            span_count,
            ",".join(self._header_keys),
        )
        try:
            result = self._exporter.export(spans)
        except Exception:
            logger.exception(
                "%s export failed: endpoint=%s service_name=%s spans=%s",
                self._name,
                self._endpoint,
                self._service_name or "",
                span_count,
            )
            raise
        logger.info(
            "%s export result: endpoint=%s service_name=%s spans=%s result=%s",
            self._name,
            self._endpoint,
            self._service_name or "",
            span_count,
            result,
        )
        return result

    def shutdown(self) -> None:
        logger.info("%s exporter shutdown: endpoint=%s", self._name, self._endpoint)
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        logger.info(
            "%s exporter force_flush: endpoint=%s timeout_millis=%s",
            self._name,
            self._endpoint,
            timeout_millis,
        )
        return self._exporter.force_flush(timeout_millis)


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
        headers[key] = _decode_otlp_header_value(key, value)
    return headers


def _decode_otlp_header_value(key: str, value: str) -> str:
    decoded = unquote(value.strip())
    if key.strip().lower() != "authorization":
        return decoded

    for scheme in ("Basic", "Bearer"):
        prefix = f"{scheme}+"
        if decoded.startswith(prefix):
            return f"{scheme} {decoded[len(prefix) :]}"
    return decoded


def _derive_otlp_traces_endpoint(endpoint: str) -> str:
    """Derive the HTTP trace endpoint from a generic OTLP endpoint."""
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return f"{endpoint}/v1/traces"


def _env_flag_enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _has_header(headers: dict[str, str], header_name: str) -> bool:
    expected = header_name.strip().lower()
    return any(key.strip().lower() == expected for key in headers)


def _get_service_name() -> str:
    service_name = os.getenv("OTEL_SERVICE_NAME", "").strip()
    if service_name:
        return service_name

    for part in os.getenv("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() == "service.name":
            return unquote(value.strip())
    return ""


def _build_cloud_monitor_otlp_http_config() -> Optional[_OtlpHttpConfig]:
    """Build CloudMonitor OTLP HTTP traces exporter config from CLOUD_MONITOR_OTLP_* env.

    traces 专用 headers 优先于通用 headers。仅当两个 headers 环境变量整体缺失时，
    才把旧 CLOUD_MONITOR_APP_KEY 按过渡兼容翻译为 Ksc-Appkey header。
    """
    traces_endpoint = os.getenv("CLOUD_MONITOR_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("CLOUD_MONITOR_OTLP_ENDPOINT", "").strip()
    endpoint = traces_endpoint
    if not endpoint and base_endpoint:
        endpoint = _derive_otlp_traces_endpoint(base_endpoint)

    raw_traces_headers = os.getenv("CLOUD_MONITOR_OTLP_TRACES_HEADERS", "")
    raw_generic_headers = os.getenv("CLOUD_MONITOR_OTLP_HEADERS", "")
    raw_headers = raw_traces_headers.strip() or raw_generic_headers
    headers = _parse_otlp_headers(raw_headers)
    # One-version compatibility applies only when both header variables are
    # absent. A present-but-invalid traces or generic header bundle must fail
    # closed rather than silently mixing it with the legacy AppKey.
    if not raw_traces_headers.strip() and not raw_generic_headers.strip():
        app_key = os.getenv("CLOUD_MONITOR_APP_KEY", "").strip()
        if app_key:
            if not endpoint:
                logger.warning(
                    "CloudMonitor OTLP exporter skipped: CLOUD_MONITOR_APP_KEY fallback "
                    "requires CLOUD_MONITOR_OTLP_ENDPOINT or CLOUD_MONITOR_OTLP_TRACES_ENDPOINT"
                )
                return None
            headers["Ksc-Appkey"] = app_key
            logger.info(
                "CloudMonitor OTLP using deprecated CLOUD_MONITOR_APP_KEY -> Ksc-Appkey fallback; "
                "server should inject CLOUD_MONITOR_OTLP_TRACES_HEADERS or "
                "CLOUD_MONITOR_OTLP_HEADERS instead"
            )

    if not endpoint:
        logger.debug("CloudMonitor OTLP exporter not configured: missing endpoint")
        return None
    if not _has_header(headers, "Ksc-Appkey"):
        logger.warning(
            "CloudMonitor OTLP exporter skipped: Ksc-Appkey missing in "
            "CLOUD_MONITOR_OTLP_TRACES_HEADERS/CLOUD_MONITOR_OTLP_HEADERS and no "
            "CLOUD_MONITOR_APP_KEY fallback"
        )
        return None

    protocol = (
        os.getenv("CLOUD_MONITOR_OTLP_TRACES_PROTOCOL", "").strip().lower()
        or os.getenv("CLOUD_MONITOR_OTLP_PROTOCOL", "").strip().lower()
        or "http/protobuf"
    )
    if protocol != "http/protobuf":
        logger.warning(
            "CloudMonitor OTLP exporter skipped: unsupported protocol %s, expected http/protobuf",
            protocol,
        )
        return None

    service_name = _get_service_name()
    logger.info(
        "CloudMonitor OTLP config resolved: endpoint=%s protocol=%s service_name=%s header_keys=%s",
        endpoint,
        protocol,
        service_name or "",
        ",".join(sorted(headers)),
    )
    return _OtlpHttpConfig(
        endpoint=endpoint,
        headers=headers,
        protocol=protocol,
        service_name=service_name,
    )


def _build_generic_otlp_http_config() -> Optional[dict]:
    """Build generic OTLP HTTP traces exporter config from standard OTEL env.

    This path consumes only the standard OTEL_EXPORTER_OTLP_* contract. Backend
    identity and authentication are opaque to KsADK.
    """
    traces_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    endpoint = traces_endpoint
    if not endpoint and base_endpoint:
        endpoint = _derive_otlp_traces_endpoint(base_endpoint)
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

    raw_headers = os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "").strip() or os.getenv(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    headers = _parse_otlp_headers(raw_headers)
    logger.info(
        "Generic OTLP HTTP config resolved: endpoint=%s protocol=http/protobuf header_keys=%s",
        endpoint,
        ",".join(sorted(headers)),
    )
    return {
        "endpoint": endpoint,
        "headers": headers,
        "protocol": "http/protobuf",
    }


def _register_span_processor(provider: Any, processor: Any) -> None:
    provider.add_span_processor(processor)
    _managed_span_processors.append(processor)


def setup_tracing(
    enable_inmemory: bool = True,
    enable_langfuse: bool | None = None,
    langfuse_config: dict[str, Any] | None = None,
    enable_otlp: bool = False,
    otlp_endpoint: str = "localhost:4317",
    enable_adk_instrumentation: bool = True,  # Auto-instrument ADK
    **kwargs,
) -> Optional[InMemoryExporter]:
    """初始化 Tracing (支持多 Exporter)

    Args:
        enable_inmemory: 是否启用内存 Exporter (Web UI 使用)
        enable_langfuse: 已废弃并忽略；请配置标准 OTEL_EXPORTER_OTLP_*
        langfuse_config: 已废弃并忽略；认证信息放入标准 OTLP headers
        enable_otlp: 是否启用 OTLP gRPC Exporter
        otlp_endpoint: OTLP gRPC 端点地址
        enable_adk_instrumentation: 是否启用 ADK 自动插桩

    Returns:
        InMemoryExporter 实例 (用于 Web UI 获取 traces)
    """
    global _exporter_instance, _tracing_initialized, _adk_instrumented

    # 防止重复初始化
    if _tracing_initialized:
        logger.debug("Tracing already initialized, skipping")
        return _exporter_instance

    if enable_langfuse or langfuse_config:
        logger.warning(
            "enable_langfuse/langfuse_config are ignored; configure the standard "
            "OTEL_EXPORTER_OTLP_* contract instead"
        )

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry not installed, tracing disabled")
        return None

    # 检查是否已有 TracerProvider (避免覆盖)
    existing_provider = trace.get_tracer_provider()
    if existing_provider and hasattr(existing_provider, "add_span_processor"):
        provider = existing_provider
        logger.debug("Using existing TracerProvider")
    else:
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        logger.debug("Created new TracerProvider")

    # 1. InMemory Exporter (for Web UI)
    if enable_inmemory:
        exporter = InMemoryExporter(max_traces=kwargs.get("max_traces", 1000))
        _exporter_instance = exporter
        _register_span_processor(provider, SimpleSpanProcessor(exporter))
        logger.info("InMemory exporter enabled")

    # 2. Generic OTLP HTTP exporter (标准 OTEL_* —— 托管 Agent 的 Langfuse 主路)
    generic_otlp_config = _build_generic_otlp_http_config()
    if generic_otlp_config:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as HttpOTLPSpanExporter,
            )

            raw_otlp_exporter = HttpOTLPSpanExporter(
                endpoint=generic_otlp_config["endpoint"],
                headers=generic_otlp_config["headers"],
            )
            otlp_exporter = _LoggingSpanExporter(
                raw_otlp_exporter,
                name="Generic OTLP",
                endpoint=generic_otlp_config["endpoint"],
                service_name=_get_service_name(),
                header_keys=sorted(generic_otlp_config["headers"]),
            )
            _register_span_processor(
                provider,
                BatchSpanProcessor(otlp_exporter, **_batch_span_processor_kwargs()),
            )
            logger.info(
                "Generic OTLP HTTP exporter enabled: %s (%s) headers=%s",
                generic_otlp_config["endpoint"],
                generic_otlp_config["protocol"],
                ",".join(sorted(generic_otlp_config["headers"])),
            )
        except ImportError as e:
            logger.warning(f"Generic OTLP HTTP exporter not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize generic OTLP HTTP exporter: {e}")

    # 3. CloudMonitor OTLP HTTP exporter (次路 —— 平台 CLOUD_MONITOR_OTLP_*)
    cloud_monitor_config = _build_cloud_monitor_otlp_http_config()
    if cloud_monitor_config:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as HttpOTLPSpanExporter,
            )

            raw_cloud_monitor_exporter = HttpOTLPSpanExporter(
                endpoint=cloud_monitor_config.endpoint,
                headers=cloud_monitor_config.headers,
            )
            cloud_monitor_exporter = _LoggingSpanExporter(
                raw_cloud_monitor_exporter,
                name="CloudMonitor OTLP",
                endpoint=cloud_monitor_config.endpoint,
                service_name=cloud_monitor_config.service_name,
                header_keys=sorted(cloud_monitor_config.headers),
            )
            _register_span_processor(
                provider,
                BatchSpanProcessor(cloud_monitor_exporter, **_batch_span_processor_kwargs()),
            )
            logger.info(
                "CloudMonitor OTLP exporter enabled: endpoint=%s protocol=%s service_name=%s",
                cloud_monitor_config.endpoint,
                cloud_monitor_config.protocol,
                cloud_monitor_config.service_name or "",
            )
        except ImportError as e:
            logger.warning(f"CloudMonitor OTLP exporter not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize CloudMonitor OTLP exporter: {e}")

    # 4. OTLP gRPC Exporter (optional compatibility path)
    if enable_otlp:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter as GrpcOTLPSpanExporter,
            )

            grpc_otlp_exporter = GrpcOTLPSpanExporter(endpoint=otlp_endpoint)
            _register_span_processor(
                provider,
                BatchSpanProcessor(grpc_otlp_exporter, **_batch_span_processor_kwargs()),
            )
            logger.info(f"OTLP exporter enabled: {otlp_endpoint}")
        except ImportError:
            logger.warning("OTLP exporter not installed")

    # 5. ADK Auto-Instrumentation (for Google ADK projects)
    if enable_adk_instrumentation and not _adk_instrumented:
        try:
            instrumentation_module = importlib.import_module(
                "openinference.instrumentation.google_adk"
            )
            GoogleADKInstrumentor = getattr(instrumentation_module, "GoogleADKInstrumentor")
            GoogleADKInstrumentor().instrument()
            _adk_instrumented = True
            logger.info("Google ADK instrumentation enabled")
        except ImportError:
            logger.debug(
                "openinference-instrumentation-google-adk not installed, "
                "ADK auto-instrumentation disabled"
            )
        except Exception as e:
            logger.debug(f"ADK instrumentation failed: {e}")

    # 6. LangChain Auto-Instrumentation (covers LangChain + LangGraph LLM/tool/node spans)
    if enable_adk_instrumentation:
        try:
            from openinference.instrumentation.langchain import LangChainInstrumentor

            LangChainInstrumentor().instrument()
            logger.info("LangChain instrumentation enabled")
        except ImportError:
            logger.warning(
                "Auto-instrumentation skipped: 'openinference-instrumentation-langchain' "
                "not installed. Install with `pip install ksadk[tracing]` for detailed traces."
            )
        except Exception as e:
            logger.error(f"LangChain instrumentation failed: {e}")

    # Register graceful shutdown
    atexit.register(shutdown_tracing)

    _tracing_initialized = True
    return _exporter_instance


def shutdown_tracing():
    """Flush and stop every processor installed by this module.

    The global provider may belong to another library, so KsADK never shuts the
    provider itself. It owns and closes only the processors it registered.
    """
    processors = list(_managed_span_processors)
    _managed_span_processors.clear()
    for processor in processors:
        try:
            processor.force_flush(timeout_millis=30000)
        except TypeError:
            try:
                processor.force_flush()
            except Exception:
                logger.exception("Tracing processor force_flush failed")
        except Exception:
            logger.exception("Tracing processor force_flush failed")
    for processor in processors:
        try:
            processor.shutdown()
        except Exception:
            logger.exception("Tracing processor shutdown failed")


def get_memory_exporter() -> Optional[InMemoryExporter]:
    """获取当前的 Memory Exporter 实例"""
    return _exporter_instance


def get_tracer(name: str = "ksadk"):
    """获取 Tracer"""
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return None
