import base64
import importlib
import sys
import types


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
    def __init__(self, exporter):
        self.exporter = exporter


class _FakeHttpOTLPSpanExporter:
    instances = []

    def __init__(self, *, endpoint, headers=None, **kwargs):
        self.endpoint = endpoint
        self.headers = headers or {}
        self.kwargs = kwargs
        self.__class__.instances.append(self)


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
