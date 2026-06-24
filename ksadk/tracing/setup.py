"""
Tracing 初始化 - 支持多 Exporter (InMemory + Langfuse + OTLP)
支持 ADK 自动插桩 via openinference-instrumentation-google-adk
"""

import atexit
import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter

logger = logging.getLogger(__name__)

_exporter_instance: Optional[InMemoryExporter] = None
_langfuse_exporter: Optional[Any] = None
_tracing_initialized: bool = False
_adk_instrumented: bool = False


@dataclass(frozen=True)
class _OtlpHttpConfig:
    endpoint: str
    headers: dict[str, str]
    protocol: str
    service_name: str


class _LoggingSpanExporter:
    """Small delegating exporter that logs external trace export flow."""

    def __init__(
        self,
        exporter: Any,
        *,
        name: str,
        endpoint: str,
        service_name: str,
        header_keys: list[str],
        span_transform: Any = None,
    ):
        self._exporter = exporter
        self._name = name
        self._endpoint = endpoint
        self._service_name = service_name
        self._header_keys = header_keys
        self._span_transform = span_transform

    def export(self, spans):
        span_count = len(spans) if hasattr(spans, "__len__") else "unknown"
        logger.info(
            "%s export started: endpoint=%s service_name=%s spans=%s headers=%s",
            self._name,
            self._endpoint,
            self._service_name or "",
            span_count,
            ",".join(self._header_keys),
        )
        try:
            export_spans = self._span_transform(spans) if self._span_transform else spans
            result = self._exporter.export(export_spans)
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

    def shutdown(self):
        shutdown = getattr(self._exporter, "shutdown", None)
        if shutdown is None:
            return None
        logger.info("%s exporter shutdown: endpoint=%s", self._name, self._endpoint)
        return shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        force_flush = getattr(self._exporter, "force_flush", None)
        if force_flush is None:
            return True
        logger.info(
            "%s exporter force_flush: endpoint=%s timeout_millis=%s",
            self._name,
            self._endpoint,
            timeout_millis,
        )
        return force_flush(timeout_millis)


def _coerce_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _span_context(span: Any) -> Any:
    context = getattr(span, "context", None)
    if context is not None:
        return context
    get_context = getattr(span, "get_span_context", None)
    if callable(get_context):
        try:
            return get_context()
        except Exception:
            return None
    return None


def _span_id(span: Any) -> Optional[int]:
    return getattr(_span_context(span), "span_id", None)


def _trace_id(span: Any) -> Optional[int]:
    return getattr(_span_context(span), "trace_id", None)


def _parent_span_id(span: Any) -> Optional[int]:
    return getattr(getattr(span, "parent", None), "span_id", None)


def _span_token_usage(span: Any) -> dict[str, int]:
    attributes = getattr(span, "attributes", None) or {}
    return {
        "input": _coerce_int(attributes.get("gen_ai.usage.input_tokens")),
        "output": _coerce_int(attributes.get("gen_ai.usage.output_tokens")),
        "cache_read": _coerce_int(attributes.get("gen_ai.usage.cache_read.input_tokens")),
        "reasoning_output": _coerce_int(
            attributes.get("gen_ai.usage.reasoning.output_tokens")
        ),
    }


def _has_nonzero_token_usage(usage: dict[str, int]) -> bool:
    return any(value > 0 for value in usage.values())


def _clone_span_with_attributes(span: Any, attributes: dict[str, Any]) -> Any:
    try:
        from opentelemetry.sdk.trace import ReadableSpan

        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=attributes,
            events=span.events,
            links=span.links,
            kind=span.kind,
            instrumentation_info=getattr(span, "instrumentation_info", None),
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=getattr(span, "instrumentation_scope", None),
        )
    except Exception:
        logger.debug("Failed to clone span for CloudMonitor token compatibility", exc_info=True)
        return span


def _prepare_cloud_monitor_spans(spans: Any) -> Any:
    """Add root token rollups for CloudMonitor without changing other exporters.

    CloudMonitor already parses gen_ai.usage.* on LLM child spans, but its root-span
    and overview APIs currently use root-level token fields. ADK/OpenInference often
    emits token usage on nested LLM spans only, and sometimes duplicates the same LLM
    usage on a parent and a child span. To avoid double counting, aggregate only
    token-bearing leaf spans and copy the result to the root span for this exporter.
    """
    if not spans:
        return spans
    span_list = list(spans)
    span_ids = {_span_id(span) for span in span_list if _span_id(span) is not None}
    if not span_ids:
        return spans

    children_by_parent: dict[tuple[Optional[int], int], list[Any]] = {}
    roots: list[Any] = []
    for span in span_list:
        sid = _span_id(span)
        if sid is None:
            continue
        parent_id = _parent_span_id(span)
        trace_id = _trace_id(span)
        if parent_id is None or parent_id not in span_ids:
            roots.append(span)
        else:
            children_by_parent.setdefault((trace_id, parent_id), []).append(span)

    token_usage_by_id = {
        (_trace_id(span), _span_id(span)): _span_token_usage(span)
        for span in span_list
        if _span_id(span) is not None
    }

    has_token_descendant_cache: dict[tuple[Optional[int], int], bool] = {}

    def has_token_descendant(span: Any) -> bool:
        sid = _span_id(span)
        key = (_trace_id(span), sid)
        if sid is None:
            return False
        if key in has_token_descendant_cache:
            return has_token_descendant_cache[key]
        for child in children_by_parent.get(key, []):
            child_key = (_trace_id(child), _span_id(child))
            if _has_nonzero_token_usage(token_usage_by_id.get(child_key, {})):
                has_token_descendant_cache[key] = True
                return True
            if has_token_descendant(child):
                has_token_descendant_cache[key] = True
                return True
        has_token_descendant_cache[key] = False
        return False

    def collect_leaf_usage(span: Any, usage: dict[str, int]) -> None:
        sid = _span_id(span)
        key = (_trace_id(span), sid)
        own_usage = token_usage_by_id.get(key, {})
        if _has_nonzero_token_usage(own_usage) and not has_token_descendant(span):
            for usage_key, value in own_usage.items():
                usage[usage_key] += value
            return
        for child in children_by_parent.get(key, []):
            collect_leaf_usage(child, usage)

    replacements: dict[int, Any] = {}
    for root in roots:
        aggregate = {"input": 0, "output": 0, "cache_read": 0, "reasoning_output": 0}
        collect_leaf_usage(root, aggregate)
        if not _has_nonzero_token_usage(aggregate):
            continue

        attributes = dict(getattr(root, "attributes", None) or {})
        updated = False
        attr_map = {
            "gen_ai.usage.input_tokens": "input",
            "gen_ai.usage.output_tokens": "output",
            "gen_ai.usage.cache_read.input_tokens": "cache_read",
            "gen_ai.usage.reasoning.output_tokens": "reasoning_output",
        }
        for attr_name, usage_key in attr_map.items():
            if _coerce_int(attributes.get(attr_name)) <= 0 and aggregate[usage_key] > 0:
                attributes[attr_name] = aggregate[usage_key]
                updated = True

        if updated:
            replacements[id(root)] = _clone_span_with_attributes(root, attributes)

    if not replacements:
        return spans
    return [replacements.get(id(span), span) for span in span_list]


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

    headers = _build_langfuse_otlp_headers(public_key, secret_key)
    return {
        "endpoint": f"{host.rstrip('/')}/api/public/otel/v1/traces",
        "headers": headers,
        "protocol": "http/protobuf",
    }


def _build_langfuse_otlp_headers(public_key: str, secret_key: str) -> dict[str, str]:
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {auth}",
        "x-langfuse-ingestion-version": "4",
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
        headers[key] = _decode_otlp_header_value(key, value)
    return headers


def _decode_otlp_header_value(key: str, value: str) -> str:
    decoded = unquote(value.strip())
    if key.strip().lower() != "authorization":
        return decoded

    for scheme in ("Basic", "Bearer"):
        prefix = f"{scheme}+"
        if decoded.startswith(prefix):
            return f"{scheme} {decoded[len(prefix):]}"
    return decoded


def _derive_otlp_traces_endpoint(endpoint: str) -> str:
    """Derive the HTTP trace endpoint from a generic OTLP endpoint."""
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return f"{endpoint}/v1/traces"


def _env_flag_disabled(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off", "disabled"}


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
    """Build CloudMonitor OTLP HTTP traces exporter config from CLOUD_MONITOR_* env."""
    enabled = os.getenv("CLOUD_MONITOR_OTLP_ENABLED", "").strip()
    if enabled and _env_flag_disabled(enabled):
        logger.info("CloudMonitor OTLP exporter disabled by CLOUD_MONITOR_OTLP_ENABLED=%s", enabled)
        return None
    cloud_monitor_langfuse_configured = all(
        os.getenv(name, "").strip()
        for name in (
            "CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY",
            "CLOUD_MONITOR_LANGFUSE_SECRET_KEY",
        )
    ) and bool(
        os.getenv("CLOUD_MONITOR_LANGFUSE_HOST", "").strip()
        or os.getenv("CLOUD_MONITOR_OTLP_ENDPOINT", "").strip()
    )
    cloud_monitor_otlp_forced = _env_flag_enabled(enabled)
    cloud_monitor_callback_requested = _env_flag_enabled(
        os.getenv("LANGFUSE_USE_CALLBACK", "")
    ) and not _env_flag_disabled(os.getenv("CLOUD_MONITOR_LANGFUSE_ENABLED", ""))
    if (
        cloud_monitor_langfuse_configured
        and cloud_monitor_callback_requested
        and not cloud_monitor_otlp_forced
    ):
        logger.info(
            "CloudMonitor OTLP exporter skipped because CloudMonitor Langfuse SDK "
            "callback is requested"
        )
        return None

    app_key = os.getenv("CLOUD_MONITOR_APP_KEY", "").strip()
    traces_endpoint = os.getenv("CLOUD_MONITOR_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("CLOUD_MONITOR_OTLP_ENDPOINT", "").strip()
    endpoint = traces_endpoint
    if not endpoint and base_endpoint:
        endpoint = _derive_otlp_traces_endpoint(base_endpoint)

    if not app_key and not endpoint:
        logger.debug("CloudMonitor OTLP exporter not configured: missing app key and endpoint")
        return None
    if not app_key:
        logger.warning("CloudMonitor OTLP exporter skipped: CLOUD_MONITOR_APP_KEY is missing")
        return None
    if not endpoint:
        logger.warning(
            "CloudMonitor OTLP exporter skipped: CLOUD_MONITOR_OTLP_ENDPOINT or "
            "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT is missing"
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

    headers = _parse_otlp_headers(os.getenv("CLOUD_MONITOR_OTLP_HEADERS", ""))
    headers["Ksc-Appkey"] = app_key
    service_name = _get_service_name()
    logger.info(
        "CloudMonitor OTLP config resolved: endpoint=%s protocol=%s service_name=%s "
        "app_key_present=%s header_keys=%s",
        endpoint,
        protocol,
        service_name or "",
        bool(app_key),
        ",".join(sorted(headers)),
    )
    return _OtlpHttpConfig(
        endpoint=endpoint,
        headers=headers,
        protocol=protocol,
        service_name=service_name,
    )


def _is_langfuse_otlp_endpoint(endpoint: str) -> bool:
    if not endpoint:
        return False
    normalized_endpoint = endpoint.strip().rstrip("/")
    langfuse_hosts = [
        os.getenv("LANGFUSE_HOST", "").strip().rstrip("/"),
        os.getenv("LANGFUSE_BASE_URL", "").strip().rstrip("/"),
    ]
    if any(host and normalized_endpoint.startswith(host) for host in langfuse_hosts):
        return True
    return "/api/public/otel" in normalized_endpoint


def _is_langfuse_callback_endpoint(endpoint: str) -> bool:
    return _env_flag_enabled(os.getenv("LANGFUSE_USE_CALLBACK", "")) and _is_langfuse_otlp_endpoint(endpoint)


def _apply_langfuse_auth_fallback(endpoint: str, headers: dict[str, str]) -> bool:
    """Add Langfuse OTLP auth when the platform provides endpoint + keys but no headers."""
    if not _is_langfuse_otlp_endpoint(endpoint) or _has_header(headers, "Authorization"):
        return False

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        logger.warning(
            "Generic OTLP endpoint looks like Langfuse but LANGFUSE_PUBLIC_KEY/SECRET_KEY "
            "are missing; Authorization header was not added"
        )
        return False

    langfuse_headers = _build_langfuse_otlp_headers(public_key, secret_key)
    headers["Authorization"] = langfuse_headers["Authorization"]
    if not _has_header(headers, "x-langfuse-ingestion-version"):
        headers["x-langfuse-ingestion-version"] = langfuse_headers[
            "x-langfuse-ingestion-version"
        ]
    return True


def _build_generic_otlp_http_config() -> Optional[dict]:
    """Build generic OTLP HTTP traces exporter config from standard OTEL env."""
    traces_endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    endpoint = traces_endpoint
    if not endpoint and base_endpoint:
        endpoint = _derive_otlp_traces_endpoint(base_endpoint)
    if not endpoint:
        return None
    if _is_langfuse_callback_endpoint(endpoint):
        logger.info(
            "Generic OTLP HTTP exporter skipped because LANGFUSE_USE_CALLBACK is enabled "
            "for endpoint=%s",
            endpoint,
        )
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
    headers = _parse_otlp_headers(raw_headers)
    langfuse_auth_fallback = _apply_langfuse_auth_fallback(endpoint, headers)
    logger.info(
        "Generic OTLP HTTP config resolved: endpoint=%s protocol=http/protobuf "
        "header_keys=%s langfuse_auth_fallback=%s",
        endpoint,
        ",".join(sorted(headers)),
        langfuse_auth_fallback,
    )
    return {
        "endpoint": endpoint,
        "headers": headers,
        "protocol": "http/protobuf",
        "langfuse_auth_fallback": langfuse_auth_fallback,
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
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
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
            otlp_exporter = _LoggingSpanExporter(
                otlp_exporter,
                name="Generic OTLP",
                endpoint=generic_otlp_config["endpoint"],
                service_name=_get_service_name(),
                header_keys=sorted(generic_otlp_config["headers"]),
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
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

    # 3. CloudMonitor OTLP HTTP exporter.
    # This uses CloudMonitor-specific env vars, so existing Langfuse / generic OTLP paths
    # continue to behave exactly as configured by the user or platform.
    cloud_monitor_config = _build_cloud_monitor_otlp_http_config()
    if cloud_monitor_config:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            cloud_monitor_exporter = OTLPSpanExporter(
                endpoint=cloud_monitor_config.endpoint,
                headers=cloud_monitor_config.headers,
            )
            cloud_monitor_exporter = _LoggingSpanExporter(
                cloud_monitor_exporter,
                name="CloudMonitor OTLP",
                endpoint=cloud_monitor_config.endpoint,
                service_name=cloud_monitor_config.service_name,
                header_keys=sorted(cloud_monitor_config.headers),
                span_transform=_prepare_cloud_monitor_spans,
            )
            provider.add_span_processor(BatchSpanProcessor(cloud_monitor_exporter))
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

    # 4. Langfuse OTLP direct exporter (auto-detect or explicit config)
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
                otlp_exporter = _LoggingSpanExporter(
                    otlp_exporter,
                    name="Langfuse OTLP",
                    endpoint=config["endpoint"],
                    service_name=_get_service_name(),
                    header_keys=sorted(config["headers"]),
                )
                provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
                logger.info(
                    "Langfuse OTLP exporter enabled: %s (%s) headers=%s",
                    config["endpoint"],
                    config["protocol"],
                    ",".join(sorted(config["headers"])),
                )
            else:
                logger.warning("Langfuse credentials not found, skipping")

        except ImportError as e:
            logger.warning(f"Langfuse OTLP exporter not available: {e}")
        except Exception as e:
            logger.error(f"Failed to initialize Langfuse OTLP exporter: {e}")
    elif langfuse_enabled:
        logger.info("Langfuse will use CallbackHandler (recommended for LangChain/LangGraph)")

    # 5. OTLP Exporter (optional)
    if enable_otlp:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info(f"OTLP exporter enabled: {otlp_endpoint}")
        except ImportError:
            logger.warning("OTLP exporter not installed")

    # 6. ADK Auto-Instrumentation (for Google ADK projects)
    if enable_adk_instrumentation and not _adk_instrumented:
        try:
            from openinference.instrumentation.google_adk import GoogleADKInstrumentor
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

    # 7. LangChain Auto-Instrumentation
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
