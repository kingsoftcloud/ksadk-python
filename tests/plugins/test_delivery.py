from __future__ import annotations

import hashlib
import json
import shutil

import httpx
import pytest

from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest
from ksadk.codex.client import CodexPluginBootstrap, codex_marketplace_tree_digest
from ksadk.plugins.artifacts import export_plugin_artifact
from ksadk.plugins.delivery import PluginDelivery, prepare_cloud_plugins, restore_delivery


@pytest.fixture
def frozen(tmp_path):
    root = tmp_path / "frozen"
    market = root / ".agents/plugins/marketplace.json"
    market.parent.mkdir(parents=True)
    market.write_text(
        json.dumps(
            {"name": "frozen-demo", "plugins": [{"name": "demo", "source": "./plugins/demo"}]}
        )
    )
    skill = root / "plugins/demo/skills/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo\n---\nUse the pinned script.")
    script = skill.parent / "run.sh"
    script.write_text("#!/bin/sh\nprintf pinned")
    script.chmod(0o755)
    manifest = {
        "name": "demo",
        "version": "1.0.0",
        "framework": "codex",
        "artifact_type": "ManagedRuntime",
        "runtime": {"name": "codex", "version": "0.147.0"},
        "plugins": [{"plugin_ref": "demo@1.0.0", "enabled": True, "components": ["skills"]}],
    }
    receipt, archive = export_plugin_artifact(
        root,
        tmp_path / "exports",
        runtime_version="0.147.0",
        plugin_lock_digest="sha256:" + "a" * 64,
    )
    delivery = PluginDelivery(
        artifact_id="12345678-1234-1234-1234-123456789abc",
        receipt=receipt,
        build_id="build_12345678",
        manifest_sha256=hashlib.sha256(serialize_managed_runtime_manifest(manifest)).hexdigest(),
        marketplace_name="frozen-demo",
        plugin_names=["demo"],
        snapshot_digest=codex_marketplace_tree_digest(root),
        bindings=manifest["plugins"],
    )
    shutil.rmtree(root)
    return manifest, delivery, archive


def test_blank_disk_restart_and_cache_tampering(frozen, tmp_path):
    _, delivery, archive = frozen
    config = restore_delivery(delivery, archive, tmp_path / "volume")
    bootstrap = CodexPluginBootstrap.from_mapping(config["codex_plugin_bootstrap"])
    bootstrap.verify()
    assert restore_delivery(delivery, archive, tmp_path / "volume") == config
    script = bootstrap.root / "plugins/demo/skills/demo/run.sh"
    assert script.stat().st_mode & 0o111 == 0o111
    script.write_text("changed after restoration")
    with pytest.raises(Exception, match="digest mismatch"):
        restore_delivery(delivery, archive, tmp_path / "volume")


@pytest.mark.parametrize("change", ["manifest", "runtime", "bindings"])
def test_pinned_declaration_cannot_drift(frozen, change):
    manifest, delivery, _ = frozen
    manifest = json.loads(json.dumps(manifest))
    if change == "runtime":
        manifest["runtime"]["version"] = "0.0.1"
    elif change == "bindings":
        manifest["plugins"][0]["components"] = ["mcp"]
    else:
        manifest["prompt"] = "another revision"
    with pytest.raises(ValueError):
        delivery.validate_manifest(manifest)


@pytest.mark.parametrize(
    "failure", [None, "truncated", "changed-receipt", "http-download", "unadmitted"]
)
def test_workload_download_is_scoped_and_precedes_activation(
    frozen, tmp_path, monkeypatch, failure
):
    manifest, delivery, archive = frozen
    monkeypatch.setenv("AGENTENGINE_PLUGIN_DELIVERY", delivery.model_dump_json())
    monkeypatch.setenv("AGENTENGINE_PLUGIN_CONTROL_URL", "https://control.example")
    monkeypatch.setenv("AGENTENGINE_PLUGIN_AGENT_ID", "ar-test")
    monkeypatch.setenv("AGENTENGINE_PLUGIN_API_KEY", "fake-workload-credential")
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "control.example":
            assert request.headers["Authorization"] == "Bearer fake-workload-credential"
            assert json.loads(request.content) == {
                "AgentId": "ar-test",
                "ManifestSHA256": delivery.manifest_sha256,
            }
            if failure == "unadmitted":
                return httpx.Response(403)
            receipt = delivery.receipt.model_dump()
            if failure == "changed-receipt":
                receipt["runtime_version"] = "0.0.1"
            return httpx.Response(
                200,
                json={
                    "Code": 0,
                    "Data": {
                        "ArtifactId": delivery.artifact_id,
                        "Receipt": receipt,
                        "DownloadUrl": ("http" if failure == "http-download" else "https")
                        + "://objects.example/archive?fake-signature",
                    },
                },
            )
        assert request.url.host == "objects.example"
        assert "Authorization" not in request.headers
        data = archive.read_bytes()
        return httpx.Response(200, content=data[:-1] if failure == "truncated" else data)

    real_client = httpx.Client
    monkeypatch.setattr(
        "ksadk.plugins.delivery.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    work = tmp_path / "workload"
    if failure:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            prepare_cloud_plugins(manifest, work)
        assert not (work / ".agentkit/cloud-plugin-launch.json").exists()
    else:
        config = prepare_cloud_plugins(manifest, work)
        text = (work / ".agentkit/cloud-plugin-launch.json").read_text()
        assert "fake-workload-credential" not in text and "fake-signature" not in text
        assert json.loads(text)["config"] == config
        assert len(calls) == 2
        # Exercise the real declaration detector and web composition boundary,
        # not just the archive helper: the adapter must receive the restored path.
        from ksadk.cli import runtime_bootstrap
        from ksadk.detection.detector import FrameworkDetector
        from ksadk.runtime import RuntimeRegistry

        (work / "agentengine.yaml").write_bytes(serialize_managed_runtime_manifest(manifest))
        monkeypatch.setenv("AGENTENGINE_MANAGED_RUNTIME", "1")
        monkeypatch.setattr(runtime_bootstrap, "build_default_runtime_registry", RuntimeRegistry)
        detection = FrameworkDetector(work).detect()
        app = runtime_bootstrap.create_runtime_web_app(detection, work)
        assert (
            app.state.runtime.launch_context.config["codex_plugin_bootstrap"]
            == config["codex_plugin_bootstrap"]
        )


def test_native_binding_without_reference_fails_before_serving(frozen, tmp_path, monkeypatch):
    from click.testing import CliRunner

    from ksadk.cli.cmd_managed_runtime import managed_runtime

    manifest, _, _ = frozen
    path = tmp_path / "agentengine.yaml"
    path.write_bytes(serialize_managed_runtime_manifest(manifest))
    monkeypatch.delenv("AGENTENGINE_PLUGIN_DELIVERY", raising=False)
    monkeypatch.setenv("AGENTENGINE_MANAGED_RUNTIME_WORKDIR", str(tmp_path / "state"))
    calls = []
    monkeypatch.setattr(
        "ksadk.cli.cmd_managed_runtime.web.callback", lambda *args: calls.append(args)
    )
    result = CliRunner().invoke(managed_runtime, [str(path)])
    assert result.exit_code != 0
    assert "admitted cloud delivery reference" in result.output
    assert not calls


def test_concurrent_workers_restore_the_same_frozen_artifact(frozen, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import ksadk.plugins.delivery as module

    _, delivery, archive = frozen
    original = module.restore_plugin_artifact
    barrier = Barrier(2)

    def restore(*args):
        result = original(*args)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(module, "restore_plugin_artifact", restore)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(restore_delivery, delivery, archive, tmp_path / "shared") for _ in range(2)
        ]
        results = [call.result(timeout=15) for call in calls]
    assert results[0] == results[1]
    CodexPluginBootstrap.from_mapping(results[0]["codex_plugin_bootstrap"]).verify()
