import base64
import importlib
import logging
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _isolate_tracing_env(monkeypatch):
    for key in (
        "CLOUD_MONITOR_APP_KEY",
        "CLOUD_MONITOR_LANGFUSE_ENABLED",
        "CLOUD_MONITOR_LANGFUSE_HOST",
        "CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY",
        "CLOUD_MONITOR_LANGFUSE_SECRET_KEY",
        "CLOUD_MONITOR_OTLP_ENABLED",
        "CLOUD_MONITOR_OTLP_ENDPOINT",
        "CLOUD_MONITOR_OTLP_HEADERS",
        "CLOUD_MONITOR_OTLP_PROTOCOL",
        "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT",
        "CLOUD_MONITOR_OTLP_TRACES_PROTOCOL",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_USE_CALLBACK",
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


class _FakeHttpOTLPSpanExporter:
    instances = []

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


class _FailingLangfuseExporter:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("legacy LangfuseExporter should not be initialized")


class _FakeLangfuseConfig:
    def __init__(self, public_key, secret_key, host="http://localhost:3000"):
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host


def _install_fake_otel(monkeypatch):
    trace_api = _FakeTraceApi()
    _FakeHttpOTLPSpanExporter.instances.clear()

    monkeypatch.setitem(sys.modules, "opentelemetry", types.SimpleNamespace(trace=trace_api))
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace",
        types.SimpleNamespace(TracerProvider=_FakeTracerProvider),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace.export",
        types.SimpleNamespace(
            SimpleSpanProcessor=_FakeSimpleSpanProcessor,
            BatchSpanProcessor=_FakeBatchSpanProcessor,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        types.SimpleNamespace(OTLPSpanExporter=_FakeHttpOTLPSpanExporter),
    )
    monkeypatch.setitem(
        sys.modules,
        "ksadk.tracing.exporters.langfuse_exporter",
        types.SimpleNamespace(
            LangfuseExporter=_FailingLangfuseExporter,
            LangfuseConfig=_FakeLangfuseConfig,
        ),
    )

    return trace_api


def _reload_setup(monkeypatch):
    setup = importlib.import_module("ksadk.tracing.setup")
    setup = importlib.reload(setup)
    monkeypatch.setattr(setup, "_tracing_initialized", False)
    monkeypatch.setattr(setup, "_exporter_instance", None)
    monkeypatch.setattr(setup, "_langfuse_exporter", None)
    monkeypatch.setattr(setup, "_adk_instrumented", False)
    return setup


def test_langfuse_env_uses_otlp_http_direct(monkeypatch):
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

    exporter = _FakeHttpOTLPSpanExporter.instances[0]
    expected_auth = base64.b64encode(b"pk-test:sk-test").decode("ascii")
    assert exporter.endpoint == "https://langfuse.pre.example.com/api/public/otel/v1/traces"
    assert exporter.headers == {
        "Authorization": f"Basic {expected_auth}",
        "x-langfuse-ingestion-version": "4",
    }
    assert setup.get_langfuse_exporter() is None
    assert len(trace_api.provider.processors) == 1


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


def test_generic_otlp_langfuse_endpoint_adds_auth_from_langfuse_env(monkeypatch):
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
    expected_auth = base64.b64encode(b"pk-test:sk-test").decode("ascii")
    assert exporter.endpoint == "https://trace-pre.agent.kspmas.ksyun.com/api/public/otel/v1/traces"
    assert exporter.headers == {
        "Authorization": f"Basic {expected_auth}",
        "x-langfuse-ingestion-version": "4",
    }
    assert len(trace_api.provider.processors) == 1
    assert trace_api.provider.processors[0].exporter._span_transform is setup._prepare_langfuse_spans


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
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example.com/v1/traces")
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
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_HEADERS", "x-extra=value%2Fwith%2Fslash")

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


def test_cloud_monitor_skips_when_app_key_missing(monkeypatch, caplog):
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
    assert "CLOUD_MONITOR_APP_KEY is missing" in caplog.text


def test_cloud_monitor_langfuse_sdk_config_keeps_otlp_by_default(monkeypatch, caplog):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "pk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_SECRET_KEY", "sk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_HOST", "https://cn-beijing-6.otlp.ksyun.com:4318")
    caplog.set_level(logging.INFO)

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert len(_FakeHttpOTLPSpanExporter.instances) == 1
    assert _FakeHttpOTLPSpanExporter.instances[0].endpoint == "https://cn-beijing-6.otlp.ksyun.com:4318/v1/traces"
    assert len(trace_api.provider.processors) == 1
    assert "CloudMonitor OTLP exporter enabled" in caplog.text


def test_cloud_monitor_langfuse_sdk_callback_mode_skips_otlp(monkeypatch, caplog):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_USE_CALLBACK", "true")
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "pk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_SECRET_KEY", "sk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_HOST", "https://cn-beijing-6.otlp.ksyun.com:4318")
    caplog.set_level(logging.INFO)

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert trace_api.provider.processors == []
    assert (
        "CloudMonitor OTLP exporter skipped because CloudMonitor Langfuse SDK "
        "callback is requested"
    ) in caplog.text


def test_cloud_monitor_otlp_can_be_forced_with_langfuse_sdk_config(monkeypatch):
    _install_fake_otel(monkeypatch)
    monkeypatch.setenv("CLOUD_MONITOR_APP_KEY", "app-key-demo")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENDPOINT", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "pk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_SECRET_KEY", "sk-cloud-monitor")
    monkeypatch.setenv("CLOUD_MONITOR_LANGFUSE_HOST", "https://cn-beijing-6.otlp.ksyun.com:4318")
    monkeypatch.setenv("CLOUD_MONITOR_OTLP_ENABLED", "true")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert len(_FakeHttpOTLPSpanExporter.instances) == 1
    assert _FakeHttpOTLPSpanExporter.instances[0].endpoint == "https://cn-beijing-6.otlp.ksyun.com:4318/v1/traces"


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


def test_cloud_monitor_exporter_rolls_leaf_token_usage_to_root(monkeypatch):
    _install_fake_otel(monkeypatch)
    setup = _reload_setup(monkeypatch)

    class _SpanContext:
        def __init__(self, trace_id, span_id):
            self.trace_id = trace_id
            self.span_id = span_id

    class _Span:
        def __init__(self, name, span_id, parent=None, attributes=None):
            self.name = name
            self.context = _SpanContext("trace-a", span_id)
            self.parent = parent
            self.attributes = attributes or {}

    def clone_span(span, attributes):
        return _Span(span.name, span.context.span_id, span.parent, attributes)

    monkeypatch.setattr(setup, "_clone_span_with_attributes", clone_span)

    root = _Span("root", 1, attributes={"existing": "yes"})
    workflow = _Span("workflow", 2, parent=root.context, attributes={})
    call_llm = _Span(
        "call_llm",
        3,
        parent=workflow.context,
        attributes={
            "gen_ai.usage.input_tokens": 105,
            "gen_ai.usage.output_tokens": 68,
            "gen_ai.usage.reasoning.output_tokens": 25,
        },
    )
    generation = _Span(
        "generate_content",
        4,
        parent=call_llm.context,
        attributes={
            "gen_ai.usage.input_tokens": 105,
            "gen_ai.usage.output_tokens": 68,
            "gen_ai.usage.reasoning.output_tokens": 25,
        },
    )

    transformed = setup._prepare_cloud_monitor_spans([root, workflow, call_llm, generation])

    transformed_root = transformed[0]
    assert transformed_root is not root
    assert transformed_root.attributes["existing"] == "yes"
    assert transformed_root.attributes["gen_ai.usage.input_tokens"] == 105
    assert transformed_root.attributes["gen_ai.usage.output_tokens"] == 68
    assert transformed_root.attributes["gen_ai.usage.reasoning.output_tokens"] == 25
    assert transformed[2] is call_llm
    assert transformed[3] is generation
    assert "gen_ai.usage.input_tokens" not in root.attributes


def test_langfuse_transform_strips_openinference_token_counts_when_ksadk_usage_exists(
    monkeypatch,
):
    _install_fake_otel(monkeypatch)
    setup = _reload_setup(monkeypatch)

    class _SpanContext:
        def __init__(self, trace_id, span_id):
            self.trace_id = trace_id
            self.span_id = span_id

    class _Span:
        def __init__(self, name, span_id, *, scope_name, attributes=None):
            self.name = name
            self.context = _SpanContext("trace-a", span_id)
            self.parent = None
            self.attributes = attributes or {}
            self.instrumentation_scope = types.SimpleNamespace(name=scope_name)

    def clone_span(span, attributes):
        return _Span(
            span.name,
            span.context.span_id,
            scope_name=span.instrumentation_scope.name,
            attributes=attributes,
        )

    monkeypatch.setattr(setup, "_clone_span_with_attributes", clone_span)

    root = _Span(
        "0611agent",
        1,
        scope_name="ksadk.conversations",
        attributes={
            "gen_ai.usage.input_tokens": 2427,
            "gen_ai.usage.output_tokens": 37,
        },
    )
    child = _Span(
        "ChatOpenAI",
        2,
        scope_name="openinference.instrumentation.langchain",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.token_count.prompt": 7860,
            "llm.token_count.completion": 108,
            "llm.token_count.total": 7968,
            "llm.model_name": "deepseek-v4-pro",
        },
    )

    transformed = setup._prepare_langfuse_spans([root, child])

    assert transformed[0] is root
    assert transformed[1] is not child
    assert transformed[1].attributes == {
        "openinference.span.kind": "CHAIN",
        "llm.model_name": "deepseek-v4-pro",
    }
    assert child.attributes["llm.token_count.prompt"] == 7860


def test_langfuse_transform_keeps_openinference_token_counts_without_ksadk_usage(
    monkeypatch,
):
    _install_fake_otel(monkeypatch)
    setup = _reload_setup(monkeypatch)

    class _SpanContext:
        def __init__(self, trace_id, span_id):
            self.trace_id = trace_id
            self.span_id = span_id

    class _Span:
        def __init__(self, name, span_id, *, scope_name, attributes=None):
            self.name = name
            self.context = _SpanContext("trace-a", span_id)
            self.parent = None
            self.attributes = attributes or {}
            self.instrumentation_scope = types.SimpleNamespace(name=scope_name)

    child = _Span(
        "ChatOpenAI",
        1,
        scope_name="openinference.instrumentation.langchain",
        attributes={
            "openinference.span.kind": "LLM",
            "llm.token_count.prompt": 7860,
        },
    )

    transformed = setup._prepare_langfuse_spans([child])

    assert transformed[0] is child
    assert transformed[0].attributes["llm.token_count.prompt"] == 7860


def test_langfuse_callback_only_skips_otlp_direct(monkeypatch):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.pre.example.com")

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=True,
        use_callback_only=True,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert trace_api.provider.processors == []


def test_langfuse_callback_only_skips_generic_otlp_to_same_langfuse_host(monkeypatch, caplog):
    trace_api = _install_fake_otel(monkeypatch)
    monkeypatch.setenv("LANGFUSE_USE_CALLBACK", "true")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://trace-pre.agent.kspmas.ksyun.com")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "https://trace-pre.agent.kspmas.ksyun.com/api/public/otel",
    )
    caplog.set_level(logging.INFO)

    setup = _reload_setup(monkeypatch)

    setup.setup_tracing(
        enable_inmemory=False,
        enable_langfuse=None,
        enable_adk_instrumentation=False,
    )

    assert _FakeHttpOTLPSpanExporter.instances == []
    assert trace_api.provider.processors == []
    assert (
        "Generic OTLP HTTP exporter skipped because LANGFUSE_USE_CALLBACK is enabled"
        in caplog.text
    )
