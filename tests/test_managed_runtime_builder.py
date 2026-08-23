from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from ksadk.builders.framework_requirements import (
    code_requirements_for_framework,
    requirements_for_framework,
)
from ksadk.builders.managed_runtime_builder import (
    ManagedRuntimeBuilder,
    managed_runtime_lock_path,
    serialize_managed_runtime_manifest,
)
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


def test_managed_runtime_builder_emits_yaml_declaration_and_lock_without_code_zip(tmp_path):
    _write_codex_project(tmp_path)

    result = ManagedRuntimeBuilder(tmp_path).build()

    assert result.success is True
    assert result.artifact_path is not None
    assert result.artifact_path.name.startswith("managed-codex-1.2.3-")
    assert result.artifact_path.name.endswith("-runtime.yaml")
    assert result.artifact_path.read_bytes()
    assert not list((tmp_path / ".agentengine" / "managed_runtime").glob("*.zip"))
    manifest_bytes = result.artifact_path.read_bytes()
    manifest = yaml.safe_load(manifest_bytes)
    lock = json.loads(managed_runtime_lock_path(result.artifact_path).read_bytes())

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


def test_managed_runtime_manifest_digest_matches_server_canonical_yaml():
    """Formatting and input key order must not change the admission digest."""

    first = {
        "name": "managed-codex",
        "prompt": "回答用户问题。\n",
        "runtime": {"version": "0.147.0", "name": "codex"},
    }
    second = {
        "runtime": {"name": "codex", "version": "0.147.0"},
        "prompt": "回答用户问题。\n",
        "name": "managed-codex",
    }

    first_bytes = serialize_managed_runtime_manifest(first)
    second_bytes = serialize_managed_runtime_manifest(second)

    assert first_bytes == second_bytes
    assert first_bytes == yaml.safe_dump(
        first,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    ).encode("utf-8")


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
    assert "artifact_type: ManagedRuntime" in runtime_config["manifest"]
    assert "ks3" not in client.create_agent.await_args.args[0]


def test_build_command_auto_selects_managed_runtime(tmp_path):
    _write_codex_project(tmp_path)

    result = CliRunner().invoke(build_command, [str(tmp_path)])

    assert result.exit_code == 0, result.output
    artifacts = list(
        (tmp_path / ".agentengine" / "managed_runtime").glob(
            "managed-codex-1.2.3-*-runtime.yaml"
        )
    )
    assert len(artifacts) == 1


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
