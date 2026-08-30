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


def test_cloud_account_credentials_persist_and_bridge_to_env(tmp_path, monkeypatch) -> None:
    """云账号 AK/SK/AccountID 配置入口:写 yaml + 桥接 env + 反映 configured 状态。

    用于 Studio -> agentengine-server 的 V4 签名请求(X-Ksc-Account-Id /
    X-Ksc-User-uuid 由 AgentEngineClient 从 AK/SK 反查或 env 注入)。
    """
    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "KSYUN_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    studio = StudioService(tmp_path / "ws")

    assert studio.get_settings()["cloudAccountConfigured"] is False

    settings = studio.update_settings(
        {
            "cloudAccessKey": "AKTEST123",
            "cloudSecretKey": "SKTEST456",
            "cloudAccountId": "10203040",
        }
    )

    assert settings["cloudAccountConfigured"] is True
    assert settings["cloudAccountId"] == "10203040"
    # 密钥不回显(本地 UI 不应回传 secret)
    assert not settings.get("cloudAccessKey")
    assert not settings.get("cloudSecretKey")
    assert os.environ["KSYUN_ACCESS_KEY"] == "AKTEST123"
    assert os.environ["KSYUN_SECRET_KEY"] == "SKTEST456"
    assert os.environ["KSYUN_ACCOUNT_ID"] == "10203040"
    # 重启(新实例)后从 yaml 回填
    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "KSYUN_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    reloaded = StudioService(tmp_path / "ws")
    assert reloaded.get_settings()["cloudAccountConfigured"] is True
    assert reloaded.get_settings()["cloudAccountId"] == "10203040"
    assert os.environ["KSYUN_ACCESS_KEY"] == "AKTEST123"


def test_cloud_account_partial_update_keeps_existing_secret(tmp_path, monkeypatch) -> None:
    """只改 AccountID 不动 AK/SK 时,已有密钥保持不变(表单留空 = 不修改)。"""
    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "KSYUN_ACCOUNT_ID"):
        monkeypatch.delenv(key, raising=False)
    studio = StudioService(tmp_path / "ws")
    studio.update_settings(
        {"cloudAccessKey": "AKTEST", "cloudSecretKey": "SKTEST", "cloudAccountId": "100"}
    )
    settings = studio.update_settings({"cloudAccountId": "200"})
    assert settings["cloudAccountConfigured"] is True
    assert settings["cloudAccountId"] == "200"
    assert os.environ["KSYUN_ACCESS_KEY"] == "AKTEST"
    assert os.environ["KSYUN_SECRET_KEY"] == "SKTEST"
    assert os.environ["KSYUN_ACCOUNT_ID"] == "200"
