import base64
import importlib
import json
import logging
import sys
import types

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags


@pytest.fixture(autouse=True)
def _isolate_tracing_env(monkeypatch):
    for key in (
        "CLOUD_MONITOR_APP_KEY",
        "CLOUD_MONITOR_OTLP_ENDPOINT",
        "CLOUD_MONITOR_OTLP_HEADERS",
        "CLOUD_MONITOR_OTLP_PROTOCOL",
        "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT",
        "CLOUD_MONITOR_OTLP_TRACES_HEADERS",
        "CLOUD_MONITOR_OTLP_TRACES_PROTOCOL",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakeTraceApi:
    def __init__(self):
        self.provider = None

    def get_tracer_provider(self):
        return None

    def set_tracer_provider(self, provider):
        self.provider = provider

    def get_tracer(self, name):
        return ("tracer", name)


class _FakeTracerProvider:
    def __init__(self):
        self.processors = []

    def add_span_processor(self, processor):
        self.processors.append(processor)


class _FakeSimpleSpanProcessor:
    def __init__(self, exporter):
        self.exporter = exporter


class _FakeBatchSpanProcessor:
    def __init__(self, exporter, **kwargs):
        self.exporter = exporter
        self.kwargs = kwargs

    def force_flush(self, timeout_millis=30000):
        self.force_flush_timeout_millis = timeout_millis
        return True

    def shutdown(self):
        self.shutdown_called = True


class _FakeHttpOTLPSpanExporter:
    instances: list["_FakeHttpOTLPSpanExporter"] = []

    def __init__(self, *, endpoint, headers=None, **kwargs):
        self.endpoint = endpoint
        self.headers = headers or {}
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def export(self, spans):
        self.exported_spans = spans
        return "SUCCESS"

    def shutdown(self):
        self.shutdown_called = True

    def force_flush(self, timeout_millis=30000):
        self.force_flush_timeout_millis = timeout_millis
        return True


def _install_fake_otel(monkeypatch):
    trace_api = _FakeTraceApi()
    _FakeHttpOTLPSpanExporter.instances.clear()

    monkeypatch.setitem(sys.modules, "opentelemetry", types.SimpleNamespace(trace=trace_api))
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace",
        types.SimpleNamespace(
            ReadableSpan=ReadableSpan,
            TracerProvider=_FakeTracerProvider,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace.export",
        types.SimpleNamespace(
            BatchSpanProcessor=_FakeBatchSpanProcessor,
            SimpleSpanProcessor=_FakeSimpleSpanProcessor,
            SpanExporter=SpanExporter,
            SpanExportResult=SpanExportResult,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        types.SimpleNamespace(OTLPSpanExporter=_FakeHttpOTLPSpanExporter),
    )

    return trace_api


def _reload_setup(monkeypatch):
    setup = importlib.import_module("ksadk.tracing.setup")
    setup = importlib.reload(setup)
    monkeypatch.setattr(setup, "_tracing_initialized", False)
    monkeypatch.setattr(setup, "_exporter_instance", None)
    monkeypatch.setattr(setup, "_adk_instrumented", False)
    monkeypatch.setattr(setup, "_managed_span_processors", [], raising=False)
    return setup


def test_langfuse_env_does_not_create_direct_exporter(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.pre.example.com/")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert trace_api.provider.processors == []


def test_generic_otlp_env_takes_precedence_over_langfuse_auto_env(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.pre.example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://collector.example.com/otel/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Bearer%20demo,x-custom=value%2Fwith%2Fslashes",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://collector.example.com/otel/v1/traces"
    assert exporter.headers == {
        "Authorization": "Bearer demo",
        "x-custom": "value/with/slashes",
    }
    assert len(_FakeHttpOTLPSpanExporter.instances) == 1
    assert len(trace_api.provider.processors) == 1


def test_generic_otlp_headers_decode_form_encoded_basic_auth(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    encoded_auth = base64.b64encode(b"pk-test:sk-test").decode("ascii")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        f"Authorization=Basic+{encoded_auth},x-langfuse-ingestion-version=4",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {
        "Authorization": f"Basic {encoded_auth}",
        "x-langfuse-ingestion-version": "4",
    }
    assert len(trace_api.provider.processors) == 1


def test_generic_otlp_never_adds_auth_from_langfuse_env(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.agent.kspmas.ksyun.com")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://trace-pre.agent.kspmas.ksyun.com/api/public/otel/v1/traces",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://trace-pre.agent.kspmas.ksyun.com/api/public/otel/v1/traces"
    assert exporter.headers == {}
    assert len(trace_api.provider.processors) == 1


def test_generic_otlp_langfuse_endpoint_keeps_existing_authorization(monkeypatch):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.agent.kspmas.ksyun.com")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://trace-pre.agent.kspmas.ksyun.com/api/public/otel/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=Bearer%20existing,x-extra=value",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {
        "Authorization": "Bearer existing",
        "x-extra": "value",
    }


def test_otlp_authorization_header_accepts_plus_between_scheme_and_value(monkeypatch):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example.com/v1/traces"
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=Basic+cGs6c2s=,x-langfuse-ingestion-version=4",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {
        "Authorization": "Basic cGs6c2s=",
        "x-langfuse-ingestion-version": "4",
    }


def test_generic_otlp_endpoint_derives_traces_path(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://collector.example.com/otel/v1/traces"
    assert exporter.headers == {}
    assert len(trace_api.provider.processors) == 1


def test_generic_otlp_traces_env_overrides_global_protocol_and_headers(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-global=ignored")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=Bearer%20trace-token,x-trace=value",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://collector.example.com/otel/v1/traces"
    assert exporter.headers == {
        "Authorization": "Bearer trace-token",
        "x-trace": "value",
    }
    assert len(trace_api.provider.processors) == 1


def test_cloud_monitor_otlp_env_adds_parallel_exporter_without_overriding_generic(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com/otel")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.pre.example.com")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "ar-demo-agent")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv(
        "CLOUD_MONITOR_OTLP_HEADERS",
        "Ksc-Appkey=app-key-demo,x-extra=value%2Fwith%2Fslash",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert len(_FakeHttpOTLPSpanExporter.instances) == 2
    generic_exporter, cloud_monitor_exporter = _FakeHttpOTLPSpanExporter.instances
    assert generic_exporter.endpoint == "https://collector.example.com/otel/v1/traces"
    assert generic_exporter.headers == {}
    assert cloud_monitor_exporter.endpoint == "https://cn-beijing-6.otlp.ksyun.com:4318/v1/traces"
    assert cloud_monitor_exporter.headers == {
        "Ksc-Appkey": "app-key-demo",
        "x-extra": "value/with/slash",
    }
    assert len(trace_api.provider.processors) == 2
    cloud_monitor_processor = trace_api.provider.processors[1]
    assert cloud_monitor_processor.exporter._name == "CloudMonitor OTLP"
    assert cloud_monitor_processor.exporter._service_name == "ar-demo-agent"


def test_cloud_monitor_headers_take_precedence_over_app_key_fallback(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "ar-demo-agent")
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "deprecated-app-key")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "Ksc-Appkey=primary-app-key")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {"Ksc-Appkey": "primary-app-key"}
    assert len(trace_api.provider.processors) == 1


def test_cloud_monitor_traces_headers_take_precedence_over_generic_headers(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "deprecated-app-key")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cloudmonitor.example.com")
    monkeypatch.setenv(
        "CLOUD_MONITOR_OTLP_HEADERS",
        "Ksc-Appkey=generic-app-key,x-route=generic",
    )
    monkeypatch.setenv(
        "CLOUD_MONITOR_OTLP_TRACES_HEADERS",
        "Ksc-Appkey=trace-app-key,x-route=traces",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {
        "Ksc-Appkey": "trace-app-key",
        "x-route": "traces",
    }
    assert len(trace_api.provider.processors) == 1


def test_cloud_monitor_invalid_traces_headers_fail_closed_without_legacy_fallback(
    monkeypatch, caplog
):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "deprecated-app-key")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cloudmonitor.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "Ksc-Appkey=generic-app-key")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_TRACES_HEADERS", "x-extra=trace-only")
    caplog.set_level(logging.WARNING)

    setup = _reload_setup(monkeypatch)
    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert "Ksc-Appkey missing" in caplog.text


def test_cloud_monitor_app_key_fallback_translates_to_header(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.headers == {"Ksc-Appkey": "app-key-demo"}
    assert len(trace_api.provider.processors) == 1


def test_cloud_monitor_app_key_fallback_is_blocked_when_headers_env_exists(monkeypatch, caplog):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "deprecated-app-key")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "x-extra=value")
    caplog.set_level(logging.WARNING)

    setup = _reload_setup(monkeypatch)
    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert "Ksc-Appkey missing" in caplog.text


def test_cloud_monitor_traces_endpoint_takes_precedence(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=resource-agent,other=value")
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://ignored.example.com:4318")
    monkeypatch.setenv(
        "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT",
        "https://cloudmonitor.example.com/custom/v1/traces",
    )

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    assert exporter.endpoint == "https://cloudmonitor.example.com/custom/v1/traces"
    assert exporter.headers == {"Ksc-Appkey": "app-key-demo"}
    assert trace_api.provider.processors[0].exporter._service_name == "resource-agent"


def test_cloud_monitor_skips_when_app_key_and_header_missing(monkeypatch, caplog):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    caplog.set_level(logging.WARNING)

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert "Ksc-Appkey missing" in caplog.text


def test_cloud_monitor_exporter_logs_export_result(monkeypatch, caplog):
    _install_fake_otel(monkeypatch)
    setup = _reload_setup(monkeypatch)
    delegate = _FakeHttpOTLPSpanExporter(
        endpoint="https://cloudmonitor.example.com/v1/traces",
        headers={"Ksc-Appkey": "app-key-demo"},
    )
    exporter = setup._LoggingSpanExporter(
        delegate,
        name="CloudMonitor OTLP",
        endpoint="https://cloudmonitor.example.com/v1/traces",
        service_name="ar-demo-agent",
        header_keys=["Ksc-Appkey"],
    )
    caplog.set_level(logging.INFO)

    result = exporter.export([object(), object()])

    assert result == "SUCCESS"
    assert "CloudMonitor OTLP export started" in caplog.text
    assert "CloudMonitor OTLP export result" in caplog.text
    assert "spans=2" in caplog.text


def test_cloud_monitor_transform_adds_compat_fields_without_mutating_source(monkeypatch):
    _install_fake_otel(monkeypatch)
    setup = _reload_setup(monkeypatch)
    resource = Resource(
        {
            "service.name": "runtime-display-name",
            "agentengine.account_id": "73398439",
            "agentengine.agent_id": "ar-20260818101245-a45271df",
            "agentengine.framework": "langgraph",
            "agentengine.langfuse_project_id": "ar20260818101245a45271df",
            "agentengine.region": "cn-beijing-6",
        }
    )
    root_context = SpanContext(1, 1, False, TraceFlags(1))
    child_context = SpanContext(1, 2, False, TraceFlags(1))
    root = ReadableSpan(
        "root",
        context=root_context,
        resource=resource,
        attributes={
            "gen_ai.agentengine.account_id": "span-value-must-win",
            "langfuse.trace.name": "demo-agent",
            "langfuse.trace.input": "hello",
            "langfuse.trace.output": "world",
            "langfuse.observation.input": "hello",
            "langfuse.observation.output": "world",
            "langfuse.session.id": "session-1",
            "session.id": "session-1",
            "langfuse.user.id": "user-1",
            "langfuse.observation.type": "agent",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "root-model",
        },
    )
    child = ReadableSpan(
        "llm",
        context=child_context,
        parent=root_context,
        resource=resource,
        attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "llm.token_count.prompt": 100,
            "llm.token_count.completion": 50,
            "llm.usage.total_tokens": 150,
            "llm.usage.cache_read.input_tokens": 20,
            "llm.usage.reasoning_tokens": 25,
        },
    )

    transformed = setup._prepare_cloud_monitor_spans([root, child])

    transformed_root, transformed_child = transformed
    assert transformed_root.context is root.context
    assert transformed_child.context is child.context
    assert transformed_child.parent is root.context
    assert (
        transformed_root.resource.attributes["service.name"]
        == resource.attributes["agentengine.agent_id"]
    )
    assert transformed_root.resource.attributes["agentengine.account_id"] == "73398439"
    for key in (
        "agentengine.account_id",
        "agentengine.agent_id",
        "agentengine.framework",
        "agentengine.langfuse_project_id",
        "agentengine.region",
    ):
        span_key = f"gen_ai.{key}"
        assert transformed_root.attributes[span_key] == (
            "span-value-must-win" if key == "agentengine.account_id" else resource.attributes[key]
        )
        assert transformed_child.attributes[span_key] == resource.attributes[key]
        assert key not in transformed_root.attributes
        assert key not in transformed_child.attributes
    assert (
        transformed_root.attributes["gen_ai.agentengine.agent_name"]
        == resource.attributes["service.name"]
    )
    assert (
        transformed_child.attributes["gen_ai.agentengine.agent_name"]
        == resource.attributes["service.name"]
    )
    for key in (
        "langfuse.trace.name",
        "langfuse.trace.input",
        "langfuse.trace.output",
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.session.id",
        "langfuse.user.id",
    ):
        assert transformed_root.attributes[key] == root.attributes[key]
    assert transformed_root.attributes["gen_ai.usage.input_tokens"] == 100
    assert transformed_root.attributes["gen_ai.usage.output_tokens"] == 50
    assert transformed_root.attributes["gen_ai.usage.total_tokens"] == 150
    assert transformed_child.attributes["gen_ai.usage.total_tokens"] == 150
    assert transformed_root.attributes["gen_ai.usage.cache_read.input_tokens"] == 20
    assert transformed_root.attributes["gen_ai.usage.reasoning.output_tokens"] == 25
    assert transformed_root.attributes["langfuse.observation.type"] == "agent"
    assert transformed_child.attributes["langfuse.observation.type"] == "generation"
    assert transformed_child.attributes["session.id"] == "session-1"
    for transformed_span, expected_model in (
        (transformed_root, "root-model"),
        (transformed_child, "gpt-4o"),
    ):
        assert transformed_span.attributes["langfuse.observation.metadata.ls_provider"] == "openai"
        assert transformed_span.attributes["langfuse.observation.model.name"] == expected_model
        assert (
            transformed_span.attributes["langfuse.observation.metadata.ls_model_name"]
            == expected_model
        )
        usage_details = json.loads(
            transformed_span.attributes["langfuse.observation.usage_details"]
        )
        assert usage_details == {
            "input": 100,
            "output": 50,
            "total": 150,
            "input_cache_read": 20,
        }
    assert root.resource.attributes["service.name"] == "runtime-display-name"
    assert root.attributes["gen_ai.agentengine.account_id"] == "span-value-must-win"
    assert "gen_ai.agentengine.agent_id" not in root.attributes
    assert "gen_ai.agentengine.agent_name" not in root.attributes
    assert "langfuse.observation.model.name" not in root.attributes
    assert "gen_ai.usage.input_tokens" not in root.attributes


def test_dual_exporters_transform_only_cloud_monitor(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://primary.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://secondary.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "Ksc-Appkey=platform")
    setup = _reload_setup(monkeypatch)
    setup.setup_tracing(enable_inmemory=False, enable_adk_instrumentation=False)
    resource = Resource(
        {
            "service.name": "runtime-display-name",
            "agentengine.agent_id": "ar-dual-export",
            "agentengine.region": "cn-beijing-6",
        }
    )
    context = SpanContext(123, 456, False, TraceFlags(1))
    source = ReadableSpan(
        "known-span",
        context=context,
        resource=resource,
        attributes={"langfuse.trace.input": "hello"},
    )

    generic_exporter, cloud_monitor_exporter = [
        processor.exporter for processor in trace_api.provider.processors
    ]
    generic_exporter.export([source])
    cloud_monitor_exporter.export([source])

    generic_span = _FakeHttpOTLPSpanExporter.instances[0].exported_spans[0]
    cloud_monitor_span = _FakeHttpOTLPSpanExporter.instances[1].exported_spans[0]
    assert generic_span is source
    assert generic_span.resource.attributes["service.name"] == "runtime-display-name"
    assert "agentengine.agent_id" not in generic_span.attributes
    assert cloud_monitor_span is not source
    assert cloud_monitor_span.context is generic_span.context
    assert cloud_monitor_span.context.trace_id == generic_span.context.trace_id == 123
    assert cloud_monitor_span.context.span_id == generic_span.context.span_id == 456
    assert cloud_monitor_span.resource.attributes["service.name"] == "ar-dual-export"
    assert cloud_monitor_span.attributes["gen_ai.agentengine.agent_id"] == "ar-dual-export"
    assert cloud_monitor_span.attributes["gen_ai.agentengine.region"] == "cn-beijing-6"
    assert cloud_monitor_span.attributes["langfuse.observation.type"] == "span"
    assert cloud_monitor_span.attributes["langfuse.trace.input"] == "hello"


def test_shutdown_flushes_and_stops_every_managed_processor(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://primary.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://secondary.example.com")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "Ksc-Appkey=platform")

    setup = _reload_setup(monkeypatch)
    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )
    processors = list(trace_api.provider.processors)
    assert len(processors) == 2

    setup.shutdown_tracing()

    assert all(processor.force_flush_timeout_millis == 30000 for processor in processors)
    assert all(processor.shutdown_called for processor in processors)
