from __future__ import annotations

import hashlib
import json
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from ksadk.builders.framework_requirements import (
    code_requirements_for_framework,
    requirements_for_framework,
)
from ksadk.builders.managed_runtime_builder import ManagedRuntimeBuilder
from ksadk.cli.cmd_build import build as build_command
from ksadk.cli.cmd_deploy import _resolve_artifact_type_input
from ksadk.cli.workflow_common import plan_artifact_build, resolve_artifact_build_plan
from ksadk.deployment.base import DeployTarget, PackageInfo
from ksadk.deployment.providers.serverless import ServerlessProvider


def _write_codex_project(tmp_path, *, runtime_version: str | None = "0.144.4"):
    runtime = {"name": "codex"}
    if runtime_version is not None:
        runtime["version"] = runtime_version
    config = {
        "name": "managed-codex",
        "version": "1.2.3",
        "framework": "codex",
        "artifact_type": "ManagedRuntime",
        "runtime": runtime,
        "model": "glm-5.2",
        "prompt": "You are a coding assistant.",
        "deploy": {"resources": {"cpu": "2"}},
    }
    (tmp_path / "agentengine.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("ksadk[codex]\n", encoding="utf-8")
    (tmp_path / "agent.py").write_text("raise RuntimeError('must not be bundled')\n")
    return config


def test_managed_runtime_builder_emits_manifest_only_bundle(tmp_path):
    _write_codex_project(tmp_path)

    result = ManagedRuntimeBuilder(tmp_path).build()

    assert result.success is True
    assert result.artifact_path is not None
    assert result.artifact_path.name == "managed-codex-1.2.3-runtime.zip"
    with zipfile.ZipFile(result.artifact_path) as archive:
        assert archive.namelist() == ["agentengine.yaml", "runtime-lock.json"]
        manifest_bytes = archive.read("agentengine.yaml")
        manifest = yaml.safe_load(manifest_bytes)
        lock = json.loads(archive.read("runtime-lock.json"))

    assert set(manifest) == {
        "name",
        "version",
        "framework",
        "artifact_type",
        "runtime",
        "model",
        "prompt",
    }
    assert manifest["artifact_type"] == "ManagedRuntime"
    assert manifest["runtime"] == {"name": "codex", "version": "0.144.4"}
    assert lock == {
        "schema_version": "runtime-manifest/v1",
        "runtime": {"name": "codex", "version": "0.144.4"},
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def test_managed_runtime_builder_requires_resolved_version(tmp_path):
    _write_codex_project(tmp_path, runtime_version=None)

    result = ManagedRuntimeBuilder(tmp_path).build()

    assert result.success is False
    assert "runtime.version" in (result.error_message or "")


def test_managed_runtime_dependency_policy_keeps_codex_out_of_code_zip():
    assert code_requirements_for_framework("codex") == []
    assert requirements_for_framework("codex") == ["openai-codex==0.144.4"]


def test_deploy_resolves_managed_runtime_from_config():
    config = {"framework": "codex", "artifact_type": "ManagedRuntime"}

    assert _resolve_artifact_type_input(config, None) == "ManagedRuntime"
    assert _resolve_artifact_type_input(config, "Container") == "Container"


def test_managed_runtime_deploy_builds_a_local_manifest_without_ks3_artifact_reference():
    plan = plan_artifact_build(
        target="serverless",
        artifact_type="ManagedRuntime",
        ks3_path=None,
        image=None,
        no_cache=False,
    )
    external = plan_artifact_build(
        target="serverless",
        artifact_type="ManagedRuntime",
        ks3_path="ks3://bucket/agent/runtime.zip",
        image=None,
        no_cache=False,
    )

    assert plan.should_build is True
    assert plan.should_publish is False
    assert external.should_build is True
    assert external.should_publish is False
    assert external.explicit_ref_option is None


def test_managed_runtime_dry_run_still_builds_the_local_manifest():
    plan = plan_artifact_build(
        target="serverless",
        artifact_type="ManagedRuntime",
        ks3_path=None,
        image=None,
        no_cache=False,
    )

    resolved = resolve_artifact_build_plan(
        plan=plan,
        target="serverless",
        artifact_type="ManagedRuntime",
        dry_run=True,
        deploy_name="managed-codex",
        region="cn-beijing-6",
        account_id=None,
        ks3_bucket=None,
        registry=None,
        explicit_reference=None,
        cached_reference=None,
    )

    assert resolved.will_build is True
    assert resolved.will_publish is False
    assert resolved.source == "built"


@pytest.mark.asyncio
async def test_serverless_managed_runtime_deploy_forwards_the_built_manifest_sha(
    tmp_path, monkeypatch
):
    _write_codex_project(tmp_path)
    provider = ServerlessProvider()
    target = DeployTarget(
        provider="serverless",
        region="cn-beijing-6",
        extra={
            "artifact_type": "ManagedRuntime",
            "runtime_name": "codex",
            "runtime_version": "0.144.4",
        },
    )
    package = PackageInfo(
        name="managed-codex",
        framework="codex",
        build_dir=str(tmp_path / ".agentengine" / "build"),
        project_dir=str(tmp_path),
    )
    await provider.build(package, target)

    client = AsyncMock()
    client.create_agent = AsyncMock(
        return_value={
            "agent_id": "ar-managed-codex",
            "name": "managed-codex",
        }
    )
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    monkeypatch.setenv("AGENTENGINE_SERVER_URL", "http://example.com")
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "2000003485")

    with patch("ksadk.deployment.providers.serverless.AgentEngineClient", return_value=client):
        await provider.deploy(package, target)

    runtime_config = client.create_agent.await_args.args[0]["runtime_config"]
    assert runtime_config["manifest_sha256"] == package.metadata["manifest_sha256"]


def test_build_command_auto_selects_managed_runtime(tmp_path):
    _write_codex_project(tmp_path)

    result = CliRunner().invoke(build_command, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (
        tmp_path / ".agentengine" / "managed_runtime" / "managed-codex-1.2.3-runtime.zip"
    ).exists()


def test_build_command_rejects_forced_code_mode_for_codex(tmp_path):
    _write_codex_project(tmp_path)

    result = CliRunner().invoke(build_command, [str(tmp_path), "--mode", "code"])

    assert result.exit_code != 0
    assert "ManagedRuntime" in result.output


def test_build_command_rejects_ks3_push_for_managed_runtime(tmp_path):
    _write_codex_project(tmp_path)

    result = CliRunner().invoke(build_command, [str(tmp_path), "--push"])

    assert result.exit_code != 0
    assert "不使用 KS3" in result.output
