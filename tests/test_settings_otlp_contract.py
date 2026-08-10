from __future__ import annotations

import ksadk.configs as configs


def test_settings_exposes_only_standard_otlp_observability(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "legacy-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "legacy-secret")
    monkeypatch.setenv("LANGFUSE_HOST", "https://legacy.invalid")

    fresh = configs.Settings()

    assert not hasattr(fresh, "langfuse")
    assert not hasattr(configs, "LangfuseConfig")
    assert not hasattr(fresh.agent, "to_langfuse_params")
    assert not hasattr(fresh.agent, "to_langfuse_metadata")
    assert fresh.otel.is_enabled is False
    assert "Langfuse" not in fresh.summary()
