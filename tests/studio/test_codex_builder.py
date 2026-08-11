from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.workspace import Workspace


def _manifest(prompt: str = "只读检查 src/demo.py，并报告一个确定的问题。\n"):
    return CodexAgentManifest.model_validate(
        {
            "name": "review-helper",
            "version": "1.0.0",
            "framework": "codex",
            "artifact_type": "ManagedRuntime",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "glm-5.2",
            "prompt": prompt,
        }
    )


def _inspector(_runtime) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


def test_build_is_a_two_file_audit_zip_with_runtime_supply_chain_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-in-artifact")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    snapshot = CodexManifestRepository(workspace).save(_manifest())

    record = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    archive = workspace.resolve(record.artifact_path, must_exist=True)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == ["agentengine.yaml", "runtime-lock.json"]
        manifest_bytes = bundle.read("agentengine.yaml")
        lock = json.loads(bundle.read("runtime-lock.json"))
        all_bytes = manifest_bytes + bundle.read("runtime-lock.json")

    assert manifest_bytes == snapshot.source_bytes
    assert lock == {
        "manifest_sha256": snapshot.manifest_sha256,
        "runtime": {"name": "codex", "version": "0.144.4"},
        "schema_version": "runtime-manifest/v1",
    }
    assert b"fixture-secret-never-in-artifact" not in all_bytes
    assert record.id == f"build_{snapshot.manifest_sha256[:20]}"
    assert record.status == "SUCCEEDED"
    assert record.manifest_sha256 == snapshot.manifest_sha256
    assert record.runtime_name == "codex"
    assert record.runtime_version == "0.144.4"
    assert record.sdk_version == "0.8.0"
    assert record.cli_version == "codex-cli 0.144.4"
    assert record.proxy_mode == "forced"
    assert record.runtime_lock == lock


def test_editing_manifest_invalidates_latest_build_and_new_build_uses_new_sha(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifests = CodexManifestRepository(workspace)
    builder = CodexStudioBuilder(workspace, runtime_inspector=_inspector)
    first_snapshot = manifests.save(_manifest())
    first = builder.build()

    second_snapshot = manifests.save(_manifest("使用新的审查规则。\n"))

    assert builder.is_current(first) is False
    second = builder.build()
    assert second.manifest_sha256 == second_snapshot.manifest_sha256
    assert second.id != first.id
    assert first.manifest_sha256 == first_snapshot.manifest_sha256
    assert builder.is_current(second) is True
