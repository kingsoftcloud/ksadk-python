from __future__ import annotations

import os
from pathlib import Path

from ksadk.studio.cloud import DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.service import StudioService


def test_persisted_sandbox_is_applied_to_env_on_service_start(tmp_path: Path, monkeypatch) -> None:
    """重启后 settings.yaml 必须回填进程环境,否则运行解析回落默认值。"""
    monkeypatch.delenv("KSADK_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    workspace = tmp_path / "ws"
    settings_dir = workspace / ".agentkit"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.yaml").write_text(
        "sandbox: workspace-write-auto\ncodexProxy: auto\n", encoding="utf-8"
    )

    StudioService(workspace)

    assert os.environ["KSADK_CODEX_SANDBOX"] == "workspace-write-auto"
    assert "KSADK_CODEX_USE_PROXY" not in os.environ


def test_codex_proxy_settings_normalize_to_runtime_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_CODEX_USE_PROXY", raising=False)
    studio = StudioService(tmp_path / "ws")

    assert studio.update_settings({"codexProxy": "forced"})["codexProxy"] == "forced"
    assert os.environ["KSADK_CODEX_USE_PROXY"] == "1"

    assert studio.update_settings({"codexProxy": "direct"})["codexProxy"] == "direct"
    assert os.environ["KSADK_CODEX_USE_PROXY"] == "0"

    assert studio.update_settings({"codexProxy": "auto"})["codexProxy"] == "auto"
    assert "KSADK_CODEX_USE_PROXY" not in os.environ


def test_codex_proxy_environment_normalizes_for_settings_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")
    assert StudioService(tmp_path / "forced").get_settings()["codexProxy"] == "forced"

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "0")
    assert StudioService(tmp_path / "direct").get_settings()["codexProxy"] == "direct"


def test_update_settings_writes_yaml_and_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_CODEX_SANDBOX", raising=False)
    studio = StudioService(tmp_path / "ws")

    settings = studio.update_settings({"sandbox": "full-access"})

    assert settings["sandbox"] == "full-access"
    assert os.environ["KSADK_CODEX_SANDBOX"] == "full-access"
    assert "full-access" in (tmp_path / "ws" / ".agentkit" / "settings.yaml").read_text(
        encoding="utf-8"
    )


def test_missing_settings_file_keeps_env_untouched(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_CODEX_SANDBOX", raising=False)

    StudioService(tmp_path / "ws")

    assert "KSADK_CODEX_SANDBOX" not in os.environ


def test_cloud_settings_use_existing_signed_account_without_persisting_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")
    monkeypatch.delenv("AGENTENGINE_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("AGENTENGINE_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("AGENTENGINE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("AGENTENGINE_REGION", raising=False)

    studio = StudioService(tmp_path / "ws")
    assert isinstance(studio.cloud.gateway, DirectAgentEngineCloudDeploymentGateway)

    settings = studio.update_settings(
        {
            "cloudRegion": "pre-online",
        }
    )

    assert settings["cloudSignedAccountConfigured"] is True
    assert isinstance(studio.cloud.gateway, DirectAgentEngineCloudDeploymentGateway)
    persisted = (tmp_path / "ws" / ".agentkit" / "settings.yaml").read_text(encoding="utf-8")
    assert "test-access-key" not in persisted
    assert "test-secret-key" not in persisted


def test_deployment_operation_scope_separates_workspace_and_cloud_account(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "account-one")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "secret-one")
    monkeypatch.setenv("AGENTENGINE_REGION", "pre-online")
    first = StudioService(tmp_path / "one").deployment_operation_scope()

    monkeypatch.setenv("KSYUN_ACCESS_KEY", "account-two")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "secret-two")
    changed_account = StudioService(tmp_path / "one").deployment_operation_scope()
    changed_workspace = StudioService(tmp_path / "two").deployment_operation_scope()

    assert first["workspace"] == changed_account["workspace"]
    assert first["cloudCredential"] != changed_account["cloudCredential"]
    assert changed_account["workspace"] != changed_workspace["workspace"]
    assert changed_account["cloudCredential"] == changed_workspace["cloudCredential"]
    assert "account-one" not in str(first)
    assert "account-two" not in str(changed_account)
