import pytest

from ksadk.studio.api_resource_connections import _binding_region


@pytest.mark.parametrize("control_region", ["cn-beijing-6", "cn-shanghai-2"])
def test_memory_service_label_resolves_to_signed_control_region(control_region):
    assert _binding_region("memory-instance", "Default-CN", control_region) == control_region


@pytest.mark.parametrize("kind,region", [
    ("knowledge-base", "Default-CN"), ("skill-space", "Default-CN"),
    ("memory-instance", "foreign-region"), ("memory-instance", ""),
    ("memory-instance", "default-cn"),
])
def test_region_mapping_never_grants_unknown_regions(kind, region):
    assert _binding_region(kind, region, "cn-beijing-6") == region


def test_memory_catalog_exposes_binding_region_and_retains_service_label(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ksadk.studio.api_resource_connections import register_resource_connection_routes

    class Client:
        region = "cn-beijing-6"

        async def list_memory_instances(self):
            return {"memory_instances": [{"id": "fixture-memory", "region": "Default-CN"}]}

        async def close(self):
            pass

    monkeypatch.setattr("ksadk.studio.api_resource_connections.AgentEngineClient", Client)
    app = FastAPI()
    register_resource_connection_routes(
        app, SimpleNamespace(resource_connections=SimpleNamespace(list=lambda: [])),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/platform-resources?kind=memory-instance")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["region"] == "cn-beijing-6"
    assert item["serviceRegion"] == "Default-CN"
