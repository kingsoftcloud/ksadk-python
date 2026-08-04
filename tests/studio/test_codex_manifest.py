from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from ksadk.studio.codex_manifest import (
    CodexAgentManifest,
    CodexManifestRepository,
)
from ksadk.studio.workspace import Workspace


def _manifest(**overrides: object) -> CodexAgentManifest:
    payload: dict[str, object] = {
        "name": "review-helper",
        "version": "1.0.0",
        "framework": "codex",
        "artifact_type": "ManagedRuntime",
        "runtime": {"name": "codex", "version": "0.144.4"},
        "model": "glm-5.2",
        "prompt": "读取目标文件，指出一个确定的问题并给出修复建议。\n",
    }
    payload.update(overrides)
    return CodexAgentManifest.model_validate(payload)


def test_repository_uses_root_agentengine_yaml_as_the_only_agent_source(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = CodexManifestRepository(workspace)

    snapshot = repository.save(_manifest())

    expected = (
        'name: review-helper\n'
        'version: 1.0.0\n'
        'framework: codex\n'
        'artifact_type: ManagedRuntime\n'
        'runtime:\n'
        '  name: codex\n'
        '  version: 0.144.4\n'
        'model: glm-5.2\n'
        'prompt: |\n'
        "  读取目标文件，指出一个确定的问题并给出修复建议。\n"
    ).encode()
    source = tmp_path / "agentengine.yaml"
    assert source.read_bytes() == expected
    assert snapshot.manifest_sha256 == hashlib.sha256(expected).hexdigest()
    assert repository.load().manifest == _manifest()
    assert not list((tmp_path / "agents").rglob("agent.yaml"))
    assert not (tmp_path / "codex.yaml").exists()


def test_editing_prompt_model_or_runtime_changes_manifest_sha(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = CodexManifestRepository(workspace)
    original = repository.save(_manifest()).manifest_sha256

    prompt_sha = repository.save(_manifest(prompt="新的审查规则。\n")).manifest_sha256
    model_sha = repository.save(_manifest(model="glm-5.3")).manifest_sha256
    runtime_sha = repository.save(
        _manifest(runtime={"name": "codex", "version": "0.145.0"})
    ).manifest_sha256

    assert len({original, prompt_sha, model_sha, runtime_sha}) == 4


def test_manifest_freezes_default_and_allowed_models_without_changing_legacy_yaml(
    tmp_path: Path,
) -> None:
    """Break caught: an Agent build cannot authorize more than its default model."""

    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = CodexManifestRepository(workspace)

    legacy = repository.save(_manifest())
    multi = repository.save(
        _manifest(models=["glm-5.2", "kimi-k2-code", "qwen3-coder"])
    )

    assert legacy.manifest.allowed_models == ("glm-5.2",)
    assert "models:" not in legacy.source_bytes.decode("utf-8")
    assert multi.manifest.allowed_models == (
        "glm-5.2",
        "kimi-k2-code",
        "qwen3-coder",
    )
    assert "models:\n- glm-5.2\n- kimi-k2-code\n- qwen3-coder\n" in (
        multi.source_bytes.decode("utf-8")
    )


def test_manifest_rejects_default_model_outside_allowed_models() -> None:
    """Break caught: omitting the default from the allowlist makes fallback unauthorized."""

    with pytest.raises(ValidationError, match="默认模型"):
        _manifest(models=["kimi-k2-code"])


def test_repository_keeps_one_yaml_manifest_per_local_agent(tmp_path: Path) -> None:
    """Break caught: saving a second Agent overwrites the root Agent YAML."""

    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = CodexManifestRepository(workspace)

    first = repository.save(_manifest(name="review-helper"))
    second = repository.save(
        _manifest(name="research-helper", prompt="执行资料研究。\n")
    )

    assert first.source_path == tmp_path / "agentengine.yaml"
    assert second.source_path == tmp_path / "agents/research-helper/agentengine.yaml"
    assert repository.load("review-helper").manifest.prompt.startswith("读取目标文件")
    assert repository.load("research-helper").manifest.prompt == "执行资料研究。\n"
    assert [item.manifest.name for item in repository.list()] == [
        "review-helper",
        "research-helper",
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"framework": "langgraph"},
        {"artifact_type": "Code"},
        {"runtime": {"name": "agentkit", "version": "1.0.0"}},
        {"api_key": "must-never-enter-the-manifest"},
        {"endpoint": "https://internal-model.example/v1"},
    ],
)
def test_manifest_rejects_non_codex_modes_and_private_connection_fields(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _manifest(**override)
