import asyncio
import json
from typing import Any, Dict

import yaml
from click.testing import CliRunner

from ksadk.api.client import AgentEngineClient, DryRunExit
from ksadk.cli import _register_commands, cli
from ksadk.cli.cmd_agent import agent
from ksadk.cli.cmd_destroy import delete as destroy_delete
from ksadk.cli.cmd_destroy import destroy as destroy_cmd
from ksadk.cli.cmd_mcp import mcp
from ksadk.cli.cmd_openclaw import openclaw
from ksadk.cli.cmd_version import version
from ksadk.cli.dry_run import run_async_with_dry_run
from ksadk.deployment.base import DeployTarget
from ksadk.deployment.providers.serverless import ServerlessProvider


class _FakeDryRunClient:
    last_init_kwargs: Dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        _FakeDryRunClient.last_init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_mcps(self, **kwargs):
        raise DryRunExit("dry-run")

    async def get_mcp(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def delete_mcp(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def list_agents(self, **kwargs):
        raise DryRunExit("dry-run")

    async def get_agent(self, **kwargs):
        raise DryRunExit("dry-run")

    async def delete_agent(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def list_versions(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def release_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def rollback_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def close(self):
        return None


class _FakeOpenClawListClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_agents(self, **_kwargs):
        return {
            "agents": [
                {
                    "agent_id": "ar-demo-1",
                    "name": "demo-openclaw",
                    "status": "running",
                    "endpoint": "https://openclaw.example.com",
                    "region": "cn-beijing-6",
                }
            ],
            "total": 145,
        }

    async def close(self):
        return None


class _FakeDeleteProvider:
    def __init__(self):
        self.calls = []

    async def destroy(self, agent_id, deploy_target):
        self.calls.append((agent_id, deploy_target))
        return True


class _FakeBatchDeleteClient:
    deleted_agents = []
    deleted_mcps = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete_agent(self, agent_id):
        self.deleted_agents.append(agent_id)
        return True

    async def delete_mcp(self, mcp_id):
        self.deleted_mcps.append(mcp_id)
        return True

    async def close(self):
        return None


class _FakeDeleteClient:
    deleted_agents = []
    should_succeed = True

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete_agent(self, agent_id):
        self.deleted_agents.append(agent_id)
        if self.should_succeed:
            return True
        raise RuntimeError("delete failed")

    async def close(self):
        return None


class _FakePartialDeleteProvider:
    def __init__(self, results: Dict[str, bool]):
        self.results = dict(results)
        self.calls = []

    async def destroy(self, agent_id, deploy_target):
        self.calls.append((agent_id, deploy_target))
        return self.results.get(agent_id, False)


def test_run_async_with_dry_run_handles_exit(capsys):
    async def _boom():
        raise DryRunExit("done")

    result = run_async_with_dry_run(_boom(), dry_run=True)
    assert result is None
    out = capsys.readouterr().out
    assert "Dry Run Completed" in out


def test_client_respects_global_dry_run_env(monkeypatch):
    monkeypatch.setenv("AGENTENGINE_GLOBAL_DRY_RUN", "1")
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="", dry_run=False)
    assert client.dry_run is True


def test_mcp_status_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        mcp,
        ["status", "mcp-123", "--dry-run"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_openclaw_list_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(openclaw, ["list", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_openclaw_list_shows_account_region_summary(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawListClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})

    result = runner.invoke(
        openclaw,
        ["list", "--region", "cn-beijing-6"],
        env={"KSYUN_ACCOUNT_ID": "2000003485"},
    )

    assert result.exit_code == 0, result.output
    assert "OpenClaw 列表" in result.output
    assert "账号: 2000003485" in result.output
    assert "region: cn-beijing-6" in result.output
    assert "总计: 145" in result.output


def test_openclaw_deploy_supports_security_profile_flags(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_deploy_openclaw(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_openclaw._deploy_openclaw", _fake_deploy_openclaw)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(openclaw, ["deploy", "--strictest"])

    assert result.exit_code == 0, result.output
    assert captured["security_profile"] == "strictest"


def test_version_list_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")

    result = runner.invoke(version, ["list", "--agent", "demo-agent", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_top_level_delete_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_delete,
        ["ar-123", "--account-id", "2000003485", "--force", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert provider.calls
    assert provider.calls[0][0] == "ar-123"


def test_top_level_destroy_accepts_yes_alias(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_cmd,
        ["ar-456", "--account-id", "2000003485", "--yes", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert provider.calls
    assert provider.calls[0][0] == "ar-456"


def test_openclaw_destroy_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(openclaw, ["destroy", "ar-demo-1", "--force", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_mcp_destroy_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        mcp,
        ["destroy", "mcp-123", "--force", "--dry-run"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_root_cli_registers_delete_alias():
    _register_commands()
    assert "agent" in cli.commands
    assert "delete" in cli.commands
    assert "destroy" in cli.commands
    assert cli.get_command(None, "delete").hidden is True
    assert cli.get_command(None, "destroy").hidden is True


def test_root_help_shows_canonical_commands_only():
    runner = CliRunner()
    _register_commands()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "agentengine agent" in result.output
    assert "agentengine status" not in result.output
    assert "agentengine invoke" not in result.output
    assert "agentengine delete" not in result.output
    assert "agentengine destroy" not in result.output


def test_agent_group_exposes_canonical_subcommands():
    runner = CliRunner()

    result = runner.invoke(agent, ["--help"])

    assert result.exit_code == 0, result.output
    assert "list" in result.output
    assert "status" in result.output
    assert "invoke" in result.output
    assert "delete" in result.output


def test_root_status_all_routes_with_compatibility_hint(monkeypatch):
    runner = CliRunner()
    _register_commands()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        cli,
        ["status", "--all", "--account-id", "2000003485", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "agentengine agent list" in result.output
    assert "Dry Run Completed" in result.output


def test_root_invoke_alias_still_callable_with_hint(monkeypatch):
    runner = CliRunner()
    _register_commands()
    invoked = {}
    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._invoke_tui",
        lambda endpoint, api_key, session_id, insecure, model, show_thinking: invoked.setdefault("endpoint", endpoint),
    )

    result = runner.invoke(cli, ["invoke", "--endpoint", "http://demo.local"])

    assert result.exit_code == 0, result.output
    assert "agentengine agent invoke" in result.output
    assert invoked["endpoint"] == "http://demo.local"


def test_legacy_root_help_points_to_canonical_commands():
    runner = CliRunner()
    _register_commands()

    result = runner.invoke(cli, ["status", "--help"])

    assert result.exit_code == 0, result.output
    assert "这是兼容入口" in result.output
    assert "agentengine agent status --help" in result.output


def test_top_level_delete_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_delete,
        ["ar-123", "ar-456", "--account-id", "2000003485", "--force", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert [call[0] for call in provider.calls] == ["ar-123", "ar-456"]


def test_top_level_destroy_supports_repeated_agent_option(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_cmd,
        ["--agent", "ar-123", "--agent", "ar-456", "--account-id", "2000003485", "--yes", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert [call[0] for call in provider.calls] == ["ar-123", "ar-456"]


def test_openclaw_destroy_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_agents = []
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(openclaw, ["destroy", "ar-demo-1", "ar-demo-2", "--force"])

    assert result.exit_code == 0, result.output
    assert _FakeBatchDeleteClient.deleted_agents == ["ar-demo-1", "ar-demo-2"]


def test_mcp_destroy_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_mcps = []
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_mcp.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(
        mcp,
        ["destroy", "mcp-123", "mcp-456", "--force"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert _FakeBatchDeleteClient.deleted_mcps == ["mcp-123", "mcp-456"]


def test_agent_delete_json_requires_yes(monkeypatch):
    runner = CliRunner()
    _register_commands()

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)

    result = runner.invoke(
        cli,
        ["--output", "json", "agent", "delete", "ar-123", "--account-id", "2000003485"],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert "--yes" in payload["error"]["message"]


def test_agent_delete_json_returns_error_on_partial_failure(monkeypatch):
    runner = CliRunner()
    _register_commands()
    provider = _FakePartialDeleteProvider({"ar-1": True, "ar-2": False})

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cli,
        ["--output", "json", "agent", "delete", "ar-1", "ar-2", "--account-id", "2000003485", "--yes"],
    )

    assert result.exit_code == 6, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "remote_error"
    assert payload["error"]["details"]["deleted"] == ["ar-1"]
    assert payload["error"]["details"]["failed"] == ["ar-2"]


def test_agent_delete_cancel_returns_cancelled_exit_code(monkeypatch):
    runner = CliRunner()
    _register_commands()

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)

    result = runner.invoke(
        cli,
        ["agent", "delete", "ar-1", "--account-id", "2000003485"],
        input="n\n",
    )

    assert result.exit_code == 7, result.output
    assert "已取消" in result.output


def test_serverless_destroy_cleans_local_state_only_after_success(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = True
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": False}),
        )
    )

    assert success is True
    assert _FakeDeleteClient.deleted_agents == ["ar-demo"]
    assert state_file.exists() is False


def test_serverless_destroy_keeps_local_state_on_dry_run(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    class _DryRunDeleteClient(_FakeDeleteClient):
        async def delete_agent(self, agent_id):
            raise DryRunExit(
                "dry-run",
                payload={"method": "POST", "url": "https://example.com", "curl": "curl -X POST https://example.com"},
            )

    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _DryRunDeleteClient)

    try:
        asyncio.run(
            provider.destroy(
                "ar-demo",
                DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": True}),
            )
        )
    except DryRunExit:
        pass
    else:
        raise AssertionError("DryRunExit should bubble for CLI handling")

    assert state_file.exists() is True


def test_serverless_destroy_keeps_local_state_when_remote_delete_fails(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = False
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": False}),
        )
    )

    assert success is False
    assert state_file.exists() is True


def test_serverless_destroy_uses_explicit_project_dir_for_state_cleanup(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()

    project_state = project_dir / ".agentengine.state"
    project_state.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    other_state = other_dir / ".agentengine.state"
    other_state.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")

    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = True
    monkeypatch.chdir(other_dir)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(
                provider="serverless",
                region="cn-beijing-6",
                extra={"dry_run": False, "project_dir": str(project_dir)},
            ),
        )
    )

    assert success is True
    assert project_state.exists() is False
    assert other_state.exists() is True
