from types import SimpleNamespace

import pytest

from ksadk.cli import runtime_bootstrap


def test_cli_managed_entrypoint_rejects_incomplete_platform_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "runtime-test")
    monkeypatch.setenv("KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED", "true")
    monkeypatch.delenv("KSADK_A2A_DATABASE_DSN", raising=False)
    detection = SimpleNamespace(type=SimpleNamespace(value="adk"), raw_config={})
    with pytest.raises(RuntimeError, match="KSADK_A2A_DATABASE_DSN"):
        runtime_bootstrap.create_runtime_web_app(detection, tmp_path)


def test_cli_managed_entrypoint_mounts_full_composition_with_adapter(monkeypatch, tmp_path):
    import ksadk.a2a.platform as platform

    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "runtime-test")
    monkeypatch.setenv("KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED", "true")
    bootstrap, adapter = object(), object()
    monkeypatch.setattr(platform, "build_managed_a2a_bootstrap_from_env", lambda: bootstrap)
    def create_adapter(executor, context):
        assert executor._managed_a2a_bootstrap is bootstrap
        return adapter
    monkeypatch.setattr(runtime_bootstrap.RuntimeExecutor, "create_adapter", create_adapter)
    monkeypatch.setattr(runtime_bootstrap, "agui_config_for_detection", lambda _: None)
    monkeypatch.setattr(runtime_bootstrap, "create_runtime_app", lambda config, _: config)
    detection = SimpleNamespace(type=SimpleNamespace(value="adk"), raw_config={})
    config = runtime_bootstrap.create_runtime_web_app(detection, tmp_path)
    assert config.a2a is bootstrap
    assert config.a2a_runtime_adapter is adapter


def test_cli_runtime_id_alone_mounts_discovery_without_full_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_A2A_RUNTIME_ID", "ar-runtime-test")
    monkeypatch.delenv("KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED", raising=False)
    monkeypatch.setattr(runtime_bootstrap, "agui_config_for_detection", lambda _: None)
    monkeypatch.setattr(runtime_bootstrap, "create_runtime_app", lambda config, _: config)
    detection = SimpleNamespace(type=SimpleNamespace(value="adk"), raw_config={})

    config = runtime_bootstrap.create_runtime_web_app(detection, tmp_path)

    assert config.a2a is not None
    assert config.a2a_runtime_adapter is None


def test_local_cli_entrypoint_does_not_require_managed_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("KSADK_A2A_RUNTIME_ID", raising=False)
    monkeypatch.setattr(runtime_bootstrap, "agui_config_for_detection", lambda _: None)
    monkeypatch.setattr(runtime_bootstrap, "create_runtime_app", lambda config, _: config)
    detection = SimpleNamespace(type=SimpleNamespace(value="adk"), raw_config={})
    config = runtime_bootstrap.create_runtime_web_app(detection, tmp_path)
    assert config.a2a is None
    assert config.a2a_runtime_adapter is None
