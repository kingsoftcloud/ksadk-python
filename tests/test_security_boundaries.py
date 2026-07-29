"""Regression tests for release security boundaries exposed by CodeQL."""

import importlib
from pathlib import Path

from ksadk.builders.container_builder import ContainerBuilder
from ksadk.model_proxy.cache import CapabilityCache, credential_scope
from ksadk.model_proxy.detect import ModelCapabilities
from ksadk.server.routes.common import _find_ui_static_asset
from ksadk.skills.service_client import SkillServiceClient


def test_kop_signing_requires_exact_tls_host():
    trusted = SkillServiceClient(base_url="https://aicp.api.ksyun.com/v1")
    spoofed = SkillServiceClient(base_url="https://aicp.api.ksyun.com.attacker.example/v1")
    plaintext = SkillServiceClient(base_url="http://aicp.api.ksyun.com/v1")

    assert trusted._is_kop_mode()
    assert not spoofed._is_kop_mode()
    assert not plaintext._is_kop_mode()


def test_kcr_optimization_rewrites_only_an_exact_registry_host(monkeypatch, tmp_path: Path):
    settings_module = importlib.import_module("ksadk.configs.settings")
    monkeypatch.setattr(settings_module, "check_endpoint_reachable", lambda *args, **kwargs: True)
    builder = ContainerBuilder(tmp_path)

    assert (
        builder._optimize_kcr_endpoint("hub.kce.ksyun.com/agentengine/demo:latest")
        == "hub-vpc.kce.ksyun.com/agentengine/demo:latest"
    )
    assert (
        builder._optimize_kcr_endpoint("registry.example/hub.kce.ksyun.com/demo:latest")
        == "registry.example/hub.kce.ksyun.com/demo:latest"
    )
    assert (
        builder._optimize_kcr_endpoint("hub.kce.ksyun.com.attacker.example/demo:latest")
        == "hub.kce.ksyun.com.attacker.example/demo:latest"
    )


def test_static_asset_lookup_cannot_escape_the_configured_bundle(tmp_path: Path):
    bundle = tmp_path / "bundle"
    asset = bundle / "assets" / "app.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("console.log('ok')", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    assert _find_ui_static_asset(bundle, "assets/app.js") == asset.resolve()
    assert _find_ui_static_asset(bundle, "../outside.txt") is None
    assert _find_ui_static_asset(bundle, "assets/../../outside.txt") is None


def test_credential_cache_scope_is_domain_separated_and_nonreversible():
    first = credential_scope("model-key-one")
    second = credential_scope("model-key-two")

    assert first != second
    assert len(first) == 32
    assert "model-key" not in first


def test_capability_cache_uses_a_prederived_scope_without_retaining_credentials():
    cache = CapabilityCache()
    calls: list[tuple[str, str]] = []

    def probe(model: str, base: str) -> ModelCapabilities:
        calls.append((model, base))
        return ModelCapabilities(verdict="unsupported")

    scope = credential_scope("model-key")
    first = cache.get_or_probe("glm-5.2", "https://gateway.example/v1", scope, probe)
    second = cache.get_or_probe("glm-5.2", "https://gateway.example/v1", scope, probe)
    assert first.verdict == "unsupported"
    assert second.verdict == "unsupported"
    assert calls == [("glm-5.2", "https://gateway.example/v1")]
