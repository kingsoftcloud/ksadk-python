import json
from types import SimpleNamespace

from ksadk.studio.contracts import ModelSpec
from ksadk.studio.dsh_models import studio_model_projection
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.workspace import Workspace


def test_studio_model_routes_share_credentials_without_persisting_secret(tmp_path, monkeypatch):
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_MODEL_NAME", "MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    workspace = Workspace(tmp_path)
    workspace.initialize()
    catalog = LocalResourceCatalog(workspace)
    for name, wire_api, credential in (("model-a", "chat", "env://TEST_KEY"), ("model-b", "chat", "env://TEST_KEY"), ("model-c", "responses", "env://RESPONSES_KEY"), ("unconfigured", "chat", "env://MISSING_KEY")):
        catalog.create_model_profile(name=name, display_name=name, version="1.0.0", description="", spec=ModelSpec(model=name, base_url="https://model.example/v1", credential_ref=credential, wire_api=wire_api))

    def resolve(ref):
        if ref == "env://MISSING_KEY":
            raise StudioError("CREDENTIAL_MISSING", "not configured")
        return "fake-test-key"

    result = studio_model_projection(catalog, SimpleNamespace(resolve=resolve))
    assert len(result.providers) == 2
    assert {p["api"] for p in result.providers.values()} == {"openai-completions", "openai-responses"}
    assert sorted(m["id"] for p in result.providers.values() for m in p["models"]) == ["model-a", "model-b", "model-c"]
    assert "fake-test-key" not in json.dumps(result.providers)
    assert "fake-test-key" not in repr(result)
    assert set(result.environment.values()) == {"fake-test-key"}
    for provider in result.providers.values():
        assert provider["apiKeyEnv"] in result.environment


def test_cli_default_is_projected_before_model_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "default-model")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    result = studio_model_projection(LocalResourceCatalog(workspace), SimpleNamespace(resolve=lambda ref: "fake-key"))
    assert result.default_model == "default-model"
    assert result.default_provider in result.providers
