"""
Tracing 初始化 - 标准 OTLP 双写（Langfuse 主 + CloudMonitor 次）

每个 Python Agent 进程一个 TracerProvider，挂两个 OTLP/HTTP protobuf BatchSpanProcessor，
消费同一批 ReadableSpan。仅为 CloudMonitor 克隆并补充兼容属性，不修改原始 span。

支持 ADK 自动插桩 via openinference-instrumentation-google-adk
"""

import atexit
import importlib
import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ksadk.tracing.exporters.inmemory_exporter import InMemoryExporter

logger = logging.getLogger(__name__)

_CLOUD_MONITOR_RESOURCE_SPAN_ATTRIBUTES = (
    "agentengine.account_id",
    "agentengine.agent_id",
    "agentengine.framework",
    "agentengine.langfuse_project_id",
    "agentengine.region",
)

_LANGFUSE_OBSERVATION_TYPES = {
    "LLM": "generation",
    "EMBEDDING": "embedding",
    "CHAIN": "chain",
    "AGENT": "agent",
    "TOOL": "tool",
    "RETRIEVER": "retriever",
    "EVALUATOR": "evaluator",
    "GUARDRAIL": "guardrail",
}


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
        span_transform: Callable[[Sequence[ReadableSpan]], Sequence[ReadableSpan]] | None = None,
    ):
        self._exporter = exporter
        self._name = name
        self._endpoint = endpoint
        self._service_name = service_name
        self._header_keys = header_keys
        self._span_transform = span_transform

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
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
    return get_context() if callable(get_context) else None


def _span_id(span: Any) -> Optional[int]:
    return getattr(_span_context(span), "span_id", None)


def _trace_id(span: Any) -> Optional[int]:
    return getattr(_span_context(span), "trace_id", None)


def _parent_span_id(span: Any) -> Optional[int]:
    return getattr(getattr(span, "parent", None), "span_id", None)


def _span_token_usage(span: Any) -> dict[str, int]:
    attributes = getattr(span, "attributes", None) or {}
    return _token_usage(attributes)


def _token_usage(attributes: dict[str, Any]) -> dict[str, int]:
    input_tokens = _coerce_int(
        attributes.get("gen_ai.usage.input_tokens")
        or attributes.get("llm.usage.prompt_tokens")
        or attributes.get("llm.token_count.prompt")
    )
    output_tokens = _coerce_int(
        attributes.get("gen_ai.usage.output_tokens")
        or attributes.get("llm.usage.completion_tokens")
        or attributes.get("llm.token_count.completion")
    )
    total_tokens = _coerce_int(
        attributes.get("gen_ai.usage.total_tokens")
        or attributes.get("llm.usage.total_tokens")
        or attributes.get("llm.token_count.total")
    )
    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens or input_tokens + output_tokens,
        "cache_read": _coerce_int(
            attributes.get("gen_ai.usage.cache_read.input_tokens")
            or attributes.get("llm.usage.cache_read.input_tokens")
        ),
        "reasoning_output": _coerce_int(
            attributes.get("gen_ai.usage.reasoning.output_tokens")
            or attributes.get("llm.usage.reasoning_tokens")
        ),
    }


def _first_span_attribute(attributes: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, ""):
            return value
    return None


def _add_langfuse_compatibility_attributes(attributes: dict[str, Any]) -> None:
    usage = _token_usage(attributes)
    observation_kind = str(attributes.get("openinference.span.kind") or "").upper()
    observation_type = _LANGFUSE_OBSERVATION_TYPES.get(observation_kind)
    if observation_type is None:
        observation_type = "generation" if _has_nonzero_token_usage(usage) else "span"
    attributes.setdefault("langfuse.observation.type", observation_type)

    provider = _first_span_attribute(
        attributes,
        "gen_ai.provider.name",
        "gen_ai.system",
        "llm.provider",
    )
    if provider is not None:
        attributes.setdefault("langfuse.observation.metadata.ls_provider", provider)

    model = _first_span_attribute(
        attributes,
        "gen_ai.request.model",
        "llm.model_name",
    )
    if model is not None:
        attributes.setdefault("langfuse.observation.model.name", model)
        attributes.setdefault("langfuse.observation.metadata.ls_model_name", model)

    if _has_nonzero_token_usage(usage):
        attributes.setdefault("gen_ai.usage.total_tokens", usage["total"])
        usage_details = {
            "input": usage["input"],
            "output": usage["output"],
            "total": usage["total"],
            "input_cache_read": usage["cache_read"],
        }
        attributes["langfuse.observation.usage_details"] = json.dumps(
            usage_details, separators=(",", ":"), sort_keys=True
        )


def _has_nonzero_token_usage(usage: dict[str, int]) -> bool:
    return any(value > 0 for value in usage.values())


def _clone_cloud_monitor_resource(resource: Resource | None) -> Resource | None:
    if resource is None:
        return None
    attributes = dict(resource.attributes)
    agent_id = str(attributes.get("agentengine.agent_id") or "").strip()
    if not agent_id or attributes.get("service.name") == agent_id:
        return resource
    attributes["service.name"] = agent_id
    return Resource(attributes, schema_url=resource.schema_url)


def _clone_span(
    span: ReadableSpan,
    *,
    attributes: dict[str, Any],
    resource: Resource | None,
) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=resource,
        attributes=attributes,
        events=span.events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=getattr(span, "instrumentation_scope", None),
    )


def _prepare_cloud_monitor_spans(
    spans: Sequence[ReadableSpan],
) -> Sequence[ReadableSpan]:
    """Clone CloudMonitor spans with resource identity and root usage compatibility."""
    if not spans:
        return spans
    span_list = list(spans)
    session_ids_by_trace: dict[Optional[int], Any] = {}
    for span in span_list:
        attributes = dict(getattr(span, "attributes", None) or {})
        session_id = _first_span_attribute(
            attributes,
            "session.id",
            "langfuse.session.id",
            "ksadk.session_id",
        )
        if session_id is not None:
            session_ids_by_trace.setdefault(_trace_id(span), session_id)
    span_keys = {
        (_trace_id(span), _span_id(span)) for span in span_list if _span_id(span) is not None
    }
    children_by_parent: dict[tuple[Optional[int], int], list[ReadableSpan]] = {}
    roots: list[ReadableSpan] = []
    for span in span_list:
        span_id = _span_id(span)
        if span_id is None:
            continue
        trace_id = _trace_id(span)
        parent_id = _parent_span_id(span)
        if parent_id is None or (trace_id, parent_id) not in span_keys:
            roots.append(span)
        else:
            children_by_parent.setdefault((trace_id, parent_id), []).append(span)

    usage_by_span = {
        (_trace_id(span), _span_id(span)): _span_token_usage(span)
        for span in span_list
        if _span_id(span) is not None
    }
    has_token_descendant_cache: dict[tuple[Optional[int], int], bool] = {}

    def has_token_descendant(span: ReadableSpan) -> bool:
        key = (_trace_id(span), _span_id(span))
        if key[1] is None:
            return False
        if key in has_token_descendant_cache:
            return has_token_descendant_cache[key]
        result = any(
            _has_nonzero_token_usage(usage_by_span.get((_trace_id(child), _span_id(child)), {}))
            or has_token_descendant(child)
            for child in children_by_parent.get(key, [])
        )
        has_token_descendant_cache[key] = result
        return result

    def collect_leaf_usage(span: ReadableSpan, aggregate: dict[str, int]) -> None:
        key = (_trace_id(span), _span_id(span))
        own_usage = usage_by_span.get(key, {})
        if _has_nonzero_token_usage(own_usage) and not has_token_descendant(span):
            for usage_key, value in own_usage.items():
                aggregate[usage_key] += value
            return
        for child in children_by_parent.get(key, []):
            collect_leaf_usage(child, aggregate)

    root_usage: dict[int, dict[str, int]] = {}
    for root in roots:
        aggregate = {
            "input": 0,
            "output": 0,
            "total": 0,
            "cache_read": 0,
            "reasoning_output": 0,
        }
        collect_leaf_usage(root, aggregate)
        root_usage[id(root)] = aggregate

    resource_cache: dict[int, Resource | None] = {}
    transformed: list[ReadableSpan] = []
    usage_attributes = {
        "gen_ai.usage.input_tokens": "input",
        "gen_ai.usage.output_tokens": "output",
        "gen_ai.usage.total_tokens": "total",
        "gen_ai.usage.cache_read.input_tokens": "cache_read",
        "gen_ai.usage.reasoning.output_tokens": "reasoning_output",
    }
    for span in span_list:
        original_resource = getattr(span, "resource", None)
        original_resource_attributes = getattr(original_resource, "attributes", {}) or {}
        resource_key = id(original_resource)
        if resource_key not in resource_cache:
            resource_cache[resource_key] = _clone_cloud_monitor_resource(original_resource)
        resource = resource_cache[resource_key]
        attributes = dict(getattr(span, "attributes", None) or {})
        resource_attributes = getattr(resource, "attributes", {}) or {}
        for key in _CLOUD_MONITOR_RESOURCE_SPAN_ATTRIBUTES:
            value = resource_attributes.get(key)
            if value not in (None, ""):
                attributes.setdefault(f"gen_ai.{key}", value)
        agent_name = original_resource_attributes.get(
            "agentengine.agent_name"
        ) or original_resource_attributes.get("service.name")
        if agent_name not in (None, ""):
            attributes.setdefault("gen_ai.agentengine.agent_name", agent_name)
        aggregate = root_usage.get(id(span), {})
        for attribute, usage_key in usage_attributes.items():
            if _coerce_int(attributes.get(attribute)) <= 0 and aggregate.get(usage_key, 0) > 0:
                attributes[attribute] = aggregate[usage_key]

        session_id = session_ids_by_trace.get(_trace_id(span))
        if session_id is not None:
            attributes.setdefault("session.id", session_id)
        _add_langfuse_compatibility_attributes(attributes)

        original_attributes = dict(getattr(span, "attributes", None) or {})
        if resource is original_resource and attributes == original_attributes:
            transformed.append(span)
        else:
            transformed.append(_clone_span(span, attributes=attributes, resource=resource))
    return transformed


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
                span_transform=_prepare_cloud_monitor_spans,
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
