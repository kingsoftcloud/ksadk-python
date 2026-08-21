from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ksadk.builders.managed_runtime_builder import managed_runtime_lock_path
from ksadk.managed_runtime import ManagedRuntimeError
from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.errors import StudioError
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


def test_build_is_a_yaml_declaration_with_runtime_supply_chain_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-in-artifact")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    snapshot = CodexManifestRepository(workspace).save(_manifest())

    record = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    declaration = workspace.resolve(record.artifact_path, must_exist=True)
    assert declaration.name.endswith("-runtime.yaml")
    assert not list(declaration.parent.glob("*.zip"))
    manifest_bytes = declaration.read_bytes()
    lock_bytes = managed_runtime_lock_path(declaration).read_bytes()
    lock = json.loads(lock_bytes)
    all_bytes = manifest_bytes + lock_bytes

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


def test_legacy_two_file_zip_receipt_remains_readable_after_yaml_only_upgrade(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    snapshot = CodexManifestRepository(workspace).save(_manifest())
    builder = CodexStudioBuilder(workspace, runtime_inspector=_inspector)
    current = builder.build()
    declaration = workspace.resolve(current.artifact_path, must_exist=True)
    legacy = declaration.with_suffix(".zip")
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr("agentengine.yaml", declaration.read_bytes())
        archive.writestr(
            "runtime-lock.json", managed_runtime_lock_path(declaration).read_bytes()
        )

    legacy_record = current.model_copy(
        update={"id": "build_legacy", "artifact_path": workspace.relative(legacy)}
    )

    assert builder.repository.manifest_text(legacy_record) == snapshot.source_bytes.decode(
        "utf-8"
    )


def test_build_surfaces_local_runtime_mismatch_as_actionable_studio_error(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())

    def unavailable_runtime(_runtime):
        raise ManagedRuntimeError("本地 codex runtime 版本为 0.147.0，配置要求 0.144.4")

    with pytest.raises(StudioError) as exc_info:
        CodexStudioBuilder(workspace, runtime_inspector=unavailable_runtime).build()

    assert exc_info.value.code == "CODEX_RUNTIME_UNAVAILABLE"
    assert exc_info.value.status_code == 422
    assert "0.147.0" in exc_info.value.message
