from __future__ import annotations

import pytest
import yaml

from ksadk.studio.configuration import WorkspaceConfiguration
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace


def test_migrates_settings_and_secrets_without_losing_partial_updates(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    workspace.atomic_write_yaml(
        ".agentkit/settings.yaml",
        {
            "sandbox": "read-only",
            "cloudAccessKey": "fixture-ak",
            "cloudSecretKey": "fixture-sk",
            "cloudAccountId": "fixture-account",
        },
    )
    workspace.atomic_write_text(".agentkit/secrets.env", "MODEL_KEY=fixture-model\n")
    config = WorkspaceConfiguration(workspace)
    config.update_settings({"cloudRegion": "cn-beijing-6"})
    CredentialResolver(workspace).put_session("ANOTHER_KEY", "fixture-other")
    reloaded = WorkspaceConfiguration(workspace)
    assert reloaded.settings()["sandbox"] == "read-only"
    assert reloaded.resolve("KSYUN_ACCESS_KEY")[0] == "fixture-ak"
    assert reloaded.resolve("MODEL_KEY")[0] == "fixture-model"
    assert reloaded.resolve("ANOTHER_KEY")[0] == "fixture-other"
    document = yaml.safe_load((tmp_path / ".agentkit/config.yaml").read_text())
    assert "cloudSecretKey" not in document["settings"]
    assert (tmp_path / ".agentkit/config.yaml").stat().st_mode & 0o777 == 0o600


def test_explicit_override_wins_without_persisting_and_dotenv_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("KSYUN_ACCESS_KEY", raising=False)
    (tmp_path / ".env").write_text("KSYUN_ACCESS_KEY=fixture-dotenv\n")
    workspace = Workspace(tmp_path)
    config = WorkspaceConfiguration(workspace, overrides={"KSYUN_ACCESS_KEY": "fixture-explicit"})
    config.update_settings({"cloudAccessKey": "fixture-saved"})
    assert config.resolve("KSYUN_ACCESS_KEY") == ("fixture-explicit", "env-file")
    assert "fixture-explicit" not in (tmp_path / ".agentkit/config.yaml").read_text()
    assert (tmp_path / ".env").read_text() == "KSYUN_ACCESS_KEY=fixture-dotenv\n"
    assert WorkspaceConfiguration(workspace).resolve("KSYUN_ACCESS_KEY")[0] == "fixture-saved"


def test_saved_credentials_create_authority_immediately_and_survive_restart(tmp_path, monkeypatch):
    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "AGENTENGINE_SERVER_URL"):
        monkeypatch.delenv(key, raising=False)
    studio = StudioService(tmp_path)
    assert studio.resource_authority is None
    settings = studio.update_settings(
        {
            "cloudAccessKey": "fixture-ak",
            "cloudSecretKey": "fixture-sk",
            "cloudServerUrl": "https://control.example.test",
        }
    )
    assert studio.resource_authority is not None
    assert studio.plugin_runs._resource_authority is studio.resource_authority
    assert settings["platformResourcesConfigured"] is True
    assert "fixture-ak" not in str(settings)
    assert "fixture-sk" not in str(settings)
    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "AGENTENGINE_SERVER_URL"):
        monkeypatch.delenv(key, raising=False)
    restarted = StudioService(tmp_path)
    assert restarted.resource_authority is not None
    assert restarted.get_settings()["cloudServerUrl"] == "https://control.example.test"


def test_bad_config_is_not_silently_replaced(tmp_path):
    directory = tmp_path / ".agentkit"
    directory.mkdir()
    path = directory / "config.yaml"
    path.write_text("version: 1\nsettings: [invalid]\n")
    with pytest.raises(Exception, match="配置"):
        WorkspaceConfiguration(Workspace(tmp_path)).update_settings({"sandbox": "read-only"})
    assert path.read_text() == "version: 1\nsettings: [invalid]\n"


@pytest.mark.asyncio
async def test_account_change_disposes_old_hosts_and_rebinds_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    for key in ("KSYUN_ACCESS_KEY", "KSYUN_SECRET_KEY", "AGENTENGINE_SERVER_URL"):
        monkeypatch.delenv(key, raising=False)
    studio = StudioService(tmp_path)
    studio.update_settings(
        {
            "cloudAccessKey": "fixture-one",
            "cloudSecretKey": "fixture-sk-one",
            "cloudServerUrl": "https://control.example.test",
            "cloudAccountId": "old-account",
        }
    )
    old_authority = studio.resource_authority
    dispose = AsyncMock()
    studio.plugin_runs._hosts["old-host"] = SimpleNamespace(host=SimpleNamespace(dispose=dispose))
    await studio.apply_settings(
        {"cloudAccessKey": "fixture-two", "cloudSecretKey": "fixture-sk-two"}
    )
    dispose.assert_awaited_once()
    assert not studio.plugin_runs._hosts
    assert studio.resource_authority is not old_authority
    assert studio.plugin_runs._resource_authority is studio.resource_authority
    assert studio.credentials.resolve("env://KSYUN_ACCESS_KEY") == "fixture-two"
    assert studio.get_settings()["cloudAccountId"] == ""
    assert studio.plugin_runs._admission_open


@pytest.mark.asyncio
async def test_busy_run_rejects_account_change_without_saving(tmp_path):
    from ksadk.studio.errors import StudioError

    studio = StudioService(tmp_path)
    before = studio.configuration.settings()
    studio.run_service._active_sessions.add(("agent", "session"))
    with pytest.raises(StudioError) as captured:
        await studio.apply_settings({"cloudAccountId": "new-account"})
    assert captured.value.code == "SETTINGS_RUNTIME_BUSY"
    assert studio.configuration.settings() == before


def test_live_credential_read_observes_settings_updates_and_deletes(tmp_path):
    workspace = Workspace(tmp_path)
    first = CredentialResolver(workspace)
    second = CredentialResolver(workspace)
    first.put_session("MODEL_KEY", "fixture-one")
    assert second.resolve("env://MODEL_KEY") == "fixture-one"
    second.put_session("MODEL_KEY", "fixture-two")
    assert first.resolve("env://MODEL_KEY") == "fixture-two"
    second.delete_session("MODEL_KEY")
    assert not first.exists("env://MODEL_KEY")


def test_source_priority_and_new_file_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_KEY", "fixture-shell")
    (tmp_path / ".env").write_text("MODEL_KEY=fixture-dotenv\nOTHER_KEY=fixture-other\n")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    configuration = WorkspaceConfiguration(workspace)
    assert configuration.resolve("MODEL_KEY") == ("fixture-shell", "environment")
    assert configuration.resolve("OTHER_KEY") == ("fixture-other", "dotenv")
    assert ".agentkit/config.yaml" in (tmp_path / ".gitignore").read_text().splitlines()


def test_invalid_endpoint_and_unpaired_account_change_leave_file_unchanged(tmp_path):
    from ksadk.studio.errors import StudioError

    studio = StudioService(tmp_path)
    studio.update_settings(
        {
            "cloudAccessKey": "fixture-ak",
            "cloudSecretKey": "fixture-sk",
            "cloudServerUrl": "https://control.example.test",
        }
    )
    path = tmp_path / ".agentkit/config.yaml"
    before = path.read_bytes()
    for payload in (
        {"cloudServerUrl": "https://user:password@example.test"},
        {"cloudAccessKey": "new-account"},
    ):
        with pytest.raises(StudioError):
            studio.update_settings(payload)
        assert path.read_bytes() == before
