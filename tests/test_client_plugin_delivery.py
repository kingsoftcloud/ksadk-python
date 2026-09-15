import hashlib
from types import SimpleNamespace

import pytest

from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest
from ksadk.plugins.artifacts import PluginArtifactReceipt
from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.workspace import Workspace


@pytest.mark.asyncio
async def test_plugin_artifact_uses_private_account_ks3_upload(tmp_path, monkeypatch):
    path = tmp_path / "artifact.zip"
    path.write_bytes(b"PK-fixture-archive")
    receipt = PluginArtifactReceipt(
        runtime_version="0.147.0",
        plugin_lock_digest="sha256:" + "a" * 64,
        artifact_digest="sha256:" + "b" * 64,
        size_bytes=path.stat().st_size,
    )
    manifest_data = {
        "runtime": {"name": "codex", "version": "0.147.0"},
        "plugins": [
            {
                "plugin_ref": "plugin://demo.plugin@1.0.0",
                "ecosystem": "codex",
                "enabled": True,
            }
        ],
    }
    manifest = serialize_managed_runtime_manifest(manifest_data).decode()
    build = SimpleNamespace(
        runtime_version="0.147.0",
        manifest_sha256=hashlib.sha256(manifest.encode()).hexdigest(),
        plugin_marketplace=SimpleNamespace(
            marketplace_name="demo-marketplace",
            plugin_names=["demo"],
            marketplace_digest="sha256:" + "c" * 64,
        ),
    )

    class Builder:
        def __init__(self, _workspace):
            self.repository = SimpleNamespace(get=lambda _build_id: build)

        def export_plugin_artifact(self, _build_id):
            return receipt, path

    calls = []

    class Uploader:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def upload(self, archive, object_key):
            calls.append(("upload", archive, object_key))
            return "ks3://agentengine-2000003485-cn-beijing-6/" + object_key

    monkeypatch.setattr("ksadk.studio.codex_builder.CodexStudioBuilder", Builder)
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=object(),
        uploader_factory=Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    delivery = (
        await gateway.prepare_plugin_delivery(
            Workspace(tmp_path), "build_12345678", manifest, "0.147.0"
        )
    )[0]
    assert delivery["artifact_id"] is None
    assert delivery["artifact_path"].startswith(
        "ks3://agentengine-2000003485-cn-beijing-6/plugin-artifacts/v1/"
    )
    assert delivery["storage_region"] == "cn-beijing-6"
    assert calls[0][1] == {
        "region": "cn-beijing-6",
        "bucket": None,
        "access_key": "test-access",
        "secret_key": "test-secret",
        "public_read": False,
    }
    assert calls[1][2] == f"plugin-artifacts/v1/{'b' * 64}.zip"
