from pathlib import Path
import os
import importlib

from ksadk.configs import setup_environment


settings_module = importlib.import_module("ksadk.configs.settings")


def test_setup_environment_mirrors_openai_model_name_to_model_name(monkeypatch, tmp_path: Path):
    (tmp_path / ".env").write_text("OPENAI_MODEL_NAME=deepseek-v3.2\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    setup_environment(tmp_path)

    assert str(Path.cwd())  # keep test explicit about no exception
    assert __import__("os").environ["OPENAI_MODEL_NAME"] == "deepseek-v3.2"
    assert __import__("os").environ["MODEL_NAME"] == "deepseek-v3.2"


def test_setup_environment_rewrites_public_openai_base_url_for_managed_runtime(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://kspmas.ksyun.com/v1")
    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-test")
    monkeypatch.delenv("KSYUN_REGION", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(settings_module, "check_endpoint_reachable", lambda *args, **kwargs: False)

    setup_environment(tmp_path)

    assert os.environ["OPENAI_BASE_URL"] == "http://kspmas-internal.sdns.ksyun.com/v1"
    assert os.environ["OPENAI_API_BASE"] == "http://kspmas-internal.sdns.ksyun.com/v1"


def test_setup_environment_injects_openai_base_url_when_only_auto_detected_base_is_available(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(settings_module, "check_endpoint_reachable", lambda *args, **kwargs: False)

    setup_environment(tmp_path)

    assert os.environ["OPENAI_BASE_URL"] == "http://kspmas-internal.sdns.ksyun.com/v1"
    assert os.environ["OPENAI_API_BASE"] == "http://kspmas-internal.sdns.ksyun.com/v1"


def test_setup_environment_does_not_force_internal_base_from_region_only(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://kspmas.ksyun.com/v1")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")
    monkeypatch.delenv("AGENT_RUNTIME_ID", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.setattr(settings_module, "check_endpoint_reachable", lambda *args, **kwargs: False)

    setup_environment(tmp_path)

    assert os.environ["OPENAI_BASE_URL"] == "http://kspmas.ksyun.com/v1"
    assert os.environ["OPENAI_API_BASE"] == "http://kspmas.ksyun.com/v1"


def test_optimize_kspmas_url_only_rewrites_exact_hostname(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ID", "ar-test")

    assert (
        settings_module.optimize_kspmas_url("https://kspmas.ksyun.com/v1?x=1")
        == "http://kspmas-internal.sdns.ksyun.com/v1?x=1"
    )
    assert (
        settings_module.optimize_kspmas_url("https://evil.example/kspmas.ksyun.com/v1")
        == "https://evil.example/kspmas.ksyun.com/v1"
    )
