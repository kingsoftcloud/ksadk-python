from pathlib import Path

import yaml
from click.testing import CliRunner

from ksadk.cli.cmd_agent import agent
from ksadk.cli.cmd_mcp import mcp
from ksadk.cli.cmd_openclaw import openclaw
from ksadk.cli.cmd_version import version


class _FakeMCPClient:
    last_name_lookup = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_mcp(self, mcp_id):
        return {
            "mcp_id": mcp_id,
            "name": "demo-mcp",
            "status": "running",
            "region": "cn-beijing-6",
            "endpoint": "https://demo.example.com",
            "mcp_endpoint": "https://demo.example.com/mcp",
            "enable_auth": True,
            "tools": ["search"],
            "created_at": "2026-03-20T12:00:00Z",
            "updated_at": "2026-03-20T12:05:00Z",
        }

    async def get_mcp_by_name(self, name, region=None):
        type(self).last_name_lookup = {"name": name, "region": region}
        return {
            "mcp_id": "mcp-by-name",
            "name": name,
            "status": "running",
            "region": region or "cn-beijing-6",
            "endpoint": "https://demo.example.com",
            "mcp_endpoint": "https://demo.example.com/mcp",
            "enable_auth": False,
            "created_at": "2026-03-20T12:00:00Z",
            "updated_at": "2026-03-20T12:05:00Z",
        }

    async def list_mcps(self, **kwargs):
        page = int(kwargs.get("page", 1))
        page_size = int(kwargs.get("page_size", 20))
        all_items = [
            {"mcp_id": "mcp-1", "name": "first", "status": "running", "mcp_endpoint": "https://demo1.example.com/mcp"},
            {"mcp_id": "mcp-2", "name": "second", "status": "failed", "mcp_endpoint": "https://demo2.example.com/mcp"},
            {"mcp_id": "mcp-3", "name": "third", "status": "creating", "mcp_endpoint": "https://demo3.example.com/mcp"},
        ]
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "mcps": all_items[start:end],
            "total": 3,
        }

    async def close(self):
        return None


class _FakeAgentStatusClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_agent(self, agent_id=None, name=None):
        return {
            "basic": {
                "agent_id": agent_id or "ar-openclaw-local",
                "name": name or "openclaw-local",
                "status": "RUNNING",
                "framework": "openclaw",
                "replicas": 1,
                "ready_replicas": 1,
            },
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
            },
            "advanced": {
                "observability_url": "https://trace.example.com/project/aropenclawlocal/traces",
            },
        }


def test_mcp_status_falls_back_to_local_state(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeMCPClient)
    state_path = tmp_path / ".agentengine.state"
    state_path.write_text(
        yaml.safe_dump({"type": "mcp", "mcp_id": "mcp-local", "region": "cn-beijing-6"}),
        encoding="utf-8",
    )

    result = runner.invoke(mcp, ["status"])

    assert result.exit_code == 0, result.output
    assert "mcp-local" in result.output
    assert "MCP 状态" in result.output


def test_agent_status_falls_back_to_openclaw_local_state(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeAgentStatusClient)
    state_path = tmp_path / ".agentengine.state"
    state_path.write_text(
        yaml.safe_dump({"type": "openclaw", "agent_id": "ar-openclaw-local", "region": "pre-online"}),
        encoding="utf-8",
    )

    result = runner.invoke(agent, ["status", "--account-id", "2000003485"])

    assert result.exit_code == 0, result.output
    assert "ar-openclaw-local" in result.output
    assert "Langfuse" in result.output
    assert "https://trace.example.com/project/aropenclawlocal/traces" in result.output


def test_mcp_list_supports_pagination(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeMCPClient)

    result = runner.invoke(mcp, ["list", "--page", "2", "--size", "1"])

    assert result.exit_code == 0, result.output
    assert "mcp-2" in result.output
    assert "mcp-1" not in result.output
    assert "MCP总数: 3  页码: 2  每页: 1" in result.output


def test_mcp_status_passes_region_to_name_lookup(monkeypatch):
    runner = CliRunner()

    class _FallbackToNameClient(_FakeMCPClient):
        last_name_lookup = None

        async def get_mcp(self, mcp_id):
            raise RuntimeError("not found")

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FallbackToNameClient)

    result = runner.invoke(mcp, ["status", "demo-mcp", "--region", "cn-shanghai-2"])

    assert result.exit_code == 0, result.output
    assert _FallbackToNameClient.last_name_lookup == {
        "name": "demo-mcp",
        "region": "cn-shanghai-2",
    }


def test_agent_list_fills_visible_page_after_filtering_openclaw(monkeypatch):
    runner = CliRunner()

    async def _fake_list_agent_runtimes(
        region,
        account_id,
        dry_run=False,
        *,
        page=1,
        page_size=20,
        framework=None,
    ):
        assert framework is None
        if page == 1:
            return {
                "agents": [
                    {
                        "agentRuntimeId": f"ar-openclaw-{idx}",
                        "agentRuntimeName": f"openclaw-{idx}",
                        "status": "RUNNING",
                        "replicas": 1,
                        "readyReplicas": 1,
                        "endpoint": f"https://openclaw-{idx}.example.com",
                        "framework": "openclaw",
                    }
                    for idx in range(100)
                ],
                "total": 101,
            }
        if page == 2:
            return {
                "agents": [
                    {
                        "agentRuntimeId": "ar-agent-1",
                        "agentRuntimeName": "visible-agent",
                        "status": "RUNNING",
                        "replicas": 1,
                        "readyReplicas": 1,
                        "endpoint": "https://agent.example.com",
                        "framework": "langgraph",
                    }
                ],
                "total": 101,
            }
        return {"agents": [], "total": 101}

    monkeypatch.setattr("ksadk.cli.cmd_status._list_agent_runtimes", _fake_list_agent_runtimes)

    result = runner.invoke(agent, ["list", "--page", "1", "--size", "1", "--account-id", "2000003485"])

    assert result.exit_code == 0, result.output
    assert "visible-agent" in result.output
    assert "Agent总数: 1  页码: 1  每页: 1" in result.output


def test_agent_list_with_explicit_openclaw_framework_does_not_hide_results(monkeypatch):
    runner = CliRunner()

    async def _fake_list_agent_runtimes(
        region,
        account_id,
        dry_run=False,
        *,
        page=1,
        page_size=20,
        framework=None,
    ):
        assert framework == "openclaw"
        return {
            "agents": [
                {
                    "agentRuntimeId": "ar-openclaw-1",
                    "agentRuntimeName": "openclaw-visible",
                    "status": "RUNNING",
                    "replicas": 1,
                    "readyReplicas": 1,
                    "endpoint": "https://openclaw.example.com",
                    "framework": "openclaw",
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr("ksadk.cli.cmd_status._list_agent_runtimes", _fake_list_agent_runtimes)

    result = runner.invoke(
        agent,
        [
            "list",
            "--page",
            "1",
            "--size",
            "20",
            "--account-id",
            "2000003485",
            "--framework",
            "openclaw",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "openclaw-visible" in result.output
    assert "已隐藏" not in result.output


def test_resource_groups_support_short_help():
    runner = CliRunner()

    for command in (mcp, openclaw, version):
        result = runner.invoke(command, ["-h"])
        assert result.exit_code == 0, result.output
