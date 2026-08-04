from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def _manifest(prompt: str = "检查 src/demo.py，只报告确定的问题。\n"):
    return CodexAgentManifest.model_validate(
        {
            "name": "review-helper",
            "version": "1.0.0",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "glm-5.2",
            "models": ["glm-5.2", "kimi-k2-code"],
            "prompt": prompt,
        }
    )


def _inspector(_runtime) -> tuple[str, str, str]:
    return "0.8.0", "0.144.4", "codex-cli 0.144.4"


def test_resolver_builds_canonical_codex_launch_context(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    spec = CodexRunSpecResolver(workspace).resolve(
        build.id,
        model="kimi-k2-code",
    )

    assert spec.launch_context.runtime_type == "codex"
    assert spec.launch_context.project_dir == tmp_path
    assert spec.agent_id == "review-helper"
    assert spec.model == "kimi-k2-code"
    assert spec.request_config == {
        "base_instructions": _manifest().prompt,
        "cwd": str(tmp_path),
    }


def test_resolver_rejects_unbound_model_before_runtime_start(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    CodexManifestRepository(workspace).save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()

    with pytest.raises(StudioError) as captured:
        CodexRunSpecResolver(workspace).resolve(build.id, model="unbound")

    assert captured.value.code == "MODEL_NOT_BOUND"


def test_resolver_rejects_stale_build_after_manifest_edit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifests = CodexManifestRepository(workspace)
    manifests.save(_manifest())
    build = CodexStudioBuilder(workspace, runtime_inspector=_inspector).build()
    manifests.save(_manifest("新 Prompt，必须重新构建。\n"))

    with pytest.raises(StudioError) as captured:
        CodexRunSpecResolver(workspace).resolve(build.id)

    assert captured.value.code == "CODEX_BUILD_STALE"
