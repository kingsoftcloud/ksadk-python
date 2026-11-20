import os
from types import SimpleNamespace

import pytest
from opentelemetry import trace

from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


@pytest.fixture
def tracing_setup(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("OTEL_", "CLOUD_MONITOR_", "LANGFUSE_")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.ProxyTracerProvider())
    calls = []
    monkeypatch.setattr("ksadk.tracing.setup_tracing", lambda: calls.append("tracing"))
    return calls


@pytest.mark.parametrize("name", [
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "CLOUD_MONITOR_OTLP_ENDPOINT", "CLOUD_MONITOR_OTLP_TRACES_ENDPOINT",
    "CLOUD_MONITOR_OTLP_HEADERS", "CLOUD_MONITOR_OTLP_TRACES_HEADERS",
    "CLOUD_MONITOR_APP_KEY",
])
def test_runtime_initializes_configured_tracing_before_route_composition(monkeypatch, tracing_setup, name):
    monkeypatch.setenv(name, "test-configuration")
    create_runtime_app(RuntimeAppConfig(), lambda *_: tracing_setup.append("routes"))
    assert tracing_setup == ["tracing", "routes"]


def test_unconfigured_runtime_does_not_initialize_tracing(tracing_setup):
    create_runtime_app(RuntimeAppConfig())
    assert tracing_setup == []


def test_legacy_langfuse_variables_do_not_implicitly_activate_export(monkeypatch, tracing_setup):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "not-an-otlp-contract")
    create_runtime_app(RuntimeAppConfig())
    assert tracing_setup == []


def test_otel_disabled_is_respected(monkeypatch, tracing_setup):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    create_runtime_app(RuntimeAppConfig())
    assert tracing_setup == []


def test_explicitly_configured_provider_is_not_reconfigured(monkeypatch, tracing_setup):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    provider = SimpleNamespace(get_tracer=lambda *_: None)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    create_runtime_app(RuntimeAppConfig())
    assert tracing_setup == []
    assert trace.get_tracer_provider() is provider


def test_explicit_no_trace_prevents_automatic_setup(monkeypatch, tracing_setup):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    create_runtime_app(RuntimeAppConfig(auto_tracing=False))
    assert tracing_setup == []


def test_exporter_setup_failure_does_not_break_runtime(monkeypatch, tracing_setup, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    def broken_setup():
        raise RuntimeError("unavailable exporter")

    monkeypatch.setattr("ksadk.tracing.setup_tracing", broken_setup)
    app = create_runtime_app(RuntimeAppConfig())
    assert app is not None
    assert "tracing" in caplog.text.lower()
