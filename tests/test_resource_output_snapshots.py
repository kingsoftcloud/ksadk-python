from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import cmd_dashboard
from ksadk.cli.cmd_mcp import mcp
from ksadk.cli.cmd_openclaw import openclaw
from ksadk.cli.cmd_version import version


SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "resource_output_snapshots.txt"


def load_section_snapshots(path: Path) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines).rstrip() + "\n"
            current_name = line[4:-4]
            current_lines = []
            continue
        current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines).rstrip() + "\n"

    return sections


def _normalize_output(text: str) -> str:
    return text.rstrip() + "\n"


class _FakeMCPClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_mcps(self, **kwargs):
        return {
            "mcps": [
                {
                    "mcp_id": "mcp-1",
                    "name": "demo-mcp",
                    "status": "running",
                    "mcp_endpoint": "https://demo.example.com/mcp",
                }
            ],
            "total": 1,
        }

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
        return await self.get_mcp(name)

    async def close(self):
        return None


class _FakeOpenClawClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_agents(self, **kwargs):
        return {
            "agents": [
                {
                    "agent_id": "ar-openclaw-1",
                    "name": "demo-openclaw",
                    "status": "running",
                    "endpoint": "https://openclaw.example.com",
                    "region": "cn-beijing-6",
                    "account_id": "2000003485",
                }
            ],
            "total": 1,
        }

    async def get_agent(self, **kwargs):
        return {
            "basic": {
                "agent_id": kwargs.get("agent_id") or "ar-openclaw-1",
                "name": "demo-openclaw",
                "status": "RUNNING",
                "framework": "openclaw",
                "region": "cn-beijing-6",
                "created_at": "2026-03-20T12:00:00Z",
                "updated_at": "2026-03-20T12:05:00Z",
            },
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
            },
            "deployment": {
                "artifact_path": "hub.kce.ksyun.com/openclaw:latest",
            },
        }

    async def close(self):
        return None


class _FakeVersionClient:
    async def list_versions(self, agent_id, page, size):
        return {
            "versions": [
                {
                    "tag": "v1.0.0",
                    "status": "current",
                    "traffic_percentage": 100,
                    "created_at": "2026-03-20T12:00:00Z",
                    "description": "Auto-released by deploy at 2026-03-20",
                }
            ],
            "total": 1,
        }

    async def close(self):
        return None


async def _fake_resolve_target_agent_id(**kwargs):
    return "ar-version-1"


async def _fake_resolve_agent_detail(*_args, **_kwargs):
    return (
        {
            "agent_id": "ar-demo",
            "name": "demo-agent",
            "framework": "langgraph",
            "endpoint": "https://agent.example.com",
        },
        type("Ref", (), {"source": "cli", "source_text": "CLI", "value": "ar-demo"})(),
        False,
    )


async def _fake_list_dashboard_access_links(**_kwargs):
    return {
        "total": 1,
        "links": [
            {
                "link_id": "lnk-1",
                "link_type": "share",
                "status": "active",
                "path": "/",
                "expires_at": None,
                "created_at": "2026-03-20T12:00:00Z",
            }
        ],
    }


async def _fake_delete_dashboard_access_link(**_kwargs):
    return {"deleted": True}


def test_resource_output_snapshots(monkeypatch):
    runner = CliRunner()
    snapshots = load_section_snapshots(SNAPSHOT_FILE)

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeMCPClient)
    result = runner.invoke(mcp, ["list"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["mcp_list"]

    result = runner.invoke(mcp, ["status", "mcp-1"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["mcp_status"]

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    result = runner.invoke(
        openclaw,
        ["list"],
        env={"KSYUN_ACCOUNT_ID": "2000003485"},
    )
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["openclaw_list"]

    result = runner.invoke(openclaw, ["status", "ar-openclaw-1"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["openclaw_status"]

    monkeypatch.setattr("ksadk.cli.cmd_version._get_client", lambda *, region, dry_run=False: _FakeVersionClient())
    monkeypatch.setattr("ksadk.cli.cmd_version._resolve_target_agent_id", _fake_resolve_target_agent_id)
    result = runner.invoke(version, ["list", "--agent", "demo-agent"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["version_list"]

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_list_dashboard_access_links", _fake_list_dashboard_access_links)
    monkeypatch.setattr(cmd_dashboard, "_delete_dashboard_access_link", _fake_delete_dashboard_access_link)
    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    result = runner.invoke(cmd_dashboard.dashboard, ["share", "list", "ar-demo"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["dashboard_share_list"]

    result = runner.invoke(cmd_dashboard.dashboard, ["share", "revoke", "lnk-1", "--yes"])
    assert result.exit_code == 0, result.output
    assert _normalize_output(result.output) == snapshots["dashboard_share_revoke"]
