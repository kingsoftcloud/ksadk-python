import json
from types import SimpleNamespace

import pytest
import requests

from ksadk.api.client import AgentEngineClient
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


@pytest.mark.asyncio
async def test_artifact_multipart_uses_existing_signed_transport(tmp_path, monkeypatch):
    client = AgentEngineClient(
        base_url="https://control.example", access_key="fake-access", secret_key="fake-secret"
    )
    path = tmp_path / "artifact.zip"
    path.write_bytes(b"PK-fixture-archive")
    receipt = {"schema_version": "ksadk.plugin-artifact/v1", "size_bytes": path.stat().st_size}
    captured = []

    def post(url, **kwargs):
        prepared = requests.Request(
            "POST", url, **{key: kwargs[key] for key in ("headers", "auth", "files", "data")}
        ).prepare()
        assert prepared.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert prepared.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert b"PK-fixture-archive" in prepared.body
        assert b'name="Receipt"' in prepared.body and json.dumps(receipt).encode() in prepared.body
        captured.append(prepared)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"Code": 0, "Data": {"ArtifactId": "test-id", "Receipt": receipt}},
        )

    monkeypatch.setattr(client, "_get_session", lambda: SimpleNamespace(post=post))
    assert await client.upload_plugin_artifact(path, receipt) == {
        "artifact_id": "test-id",
        "receipt": receipt,
    }
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_old_server_rejects_before_export_or_upload(tmp_path):
    class Client:
        async def get_plugin_delivery_capabilities(self):
            return {"deployment_admission": False}

        async def upload_plugin_artifact(self, *_):
            pytest.fail("must not upload to unsupported server")

    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="test",
        client=Client(),
        ks3_credentials={"access_key": "fixture", "secret_key": "fixture"},
    )
    with pytest.raises(StudioError, match="未上传插件"):
        await gateway.prepare_plugin_delivery(
            Workspace(tmp_path), "nonexistent-build", "not-yaml", "0.147.0"
        )
    assert not list(tmp_path.rglob("*.zip"))
