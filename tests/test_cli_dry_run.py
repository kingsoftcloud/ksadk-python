import asyncio
from typing import Any, Dict

from click.testing import CliRunner

from ksadk.api.client import AgentEngineClient, DryRunExit
from ksadk.cli.cmd_mcp import mcp
from ksadk.cli.cmd_openclaw import openclaw
from ksadk.cli.cmd_version import version
from ksadk.cli.dry_run import run_async_with_dry_run


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

    async def list_versions(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def release_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def rollback_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def close(self):
        return None


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
