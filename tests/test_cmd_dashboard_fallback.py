import json
from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import cmd_dashboard


async def _fake_resolve_agent_detail(*_args, **_kwargs):
    return (
        {
            "agent_id": "ar-test",
            "name": "demo-agent",
            "framework": "langgraph",
            "endpoint": "http://demo.example.com",
        },
        type("Ref", (), {"source": "cli", "source_text": "CLI", "value": "ar-test"})(),
        False,
    )


async def _fake_create_access_link(*_args, **_kwargs):
    return {
        "link_id": "lnk-1",
        "access_url": "http://demo.example.com/s/lnk-1",
        "expires_at": "2026-03-09T00:00:00Z",
    }


def test_dashboard_uses_access_link_by_default(monkeypatch):
    opened = {}
    captured = {}
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    async def _fake_create(*_args, **kwargs):
        captured.update(kwargs)
        return await _fake_create_access_link()

    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["ar-test"])
    assert result.exit_code == 0, result.output
    assert opened == {}
    assert captured["path"] == "/chat"
    assert "http://demo.example.com/s/lnk-1" in result.output


def test_dashboard_open_is_canonical_command(monkeypatch):
    opened = {}
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-test"])
    assert result.exit_code == 0, result.output
    assert opened == {}
    assert "http://demo.example.com/s/lnk-1" in result.output


def test_dashboard_open_resolves_openclaw_state_from_cwd(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    opened = {}

    state_path = tmp_path / ".agentengine.state"
    state_path.write_text(
        "agent_id: ar-openclaw-1\n"
        "name: demo-openclaw\n"
        "type: openclaw\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {"agent_id": "ar-openclaw-1", "name": "demo-openclaw", "type": "openclaw"},
    )

    async def _fake_resolve(_region, primary_ref, fallback_ref):
        assert primary_ref.value == "ar-openclaw-1"
        assert primary_ref.source == "state.agent_id"
        assert fallback_ref is None
        return (
            {
                "agent_id": "ar-openclaw-1",
                "name": "demo-openclaw",
                "framework": "openclaw",
                "endpoint": "http://demo.example.com",
            },
            primary_ref,
            False,
        )

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["open"])

    assert result.exit_code == 0, result.output
    assert opened == {}
    assert "未显式指定 Agent，使用 .agentengine.state 的 agent_id: ar-openclaw-1" in result.output
    assert "http://demo.example.com/s/lnk-1" in result.output


def test_dashboard_supports_share_subcommand(monkeypatch):
    runner = CliRunner()

    async def _fake_list(*_args, **_kwargs):
        return {"total": 1, "links": [{"link_id": "abc123", "link_type": "share", "status": "active", "path": "/", "expires_at": None, "created_at": "2026-03-09T00:00:00Z"}]}

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_list_dashboard_access_links", _fake_list)

    result = runner.invoke(cmd_dashboard.dashboard, ["share", "list", "ar-test"])
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output


def test_dashboard_list_is_no_longer_ambiguous():
    runner = CliRunner()

    result = runner.invoke(cmd_dashboard.dashboard, ["list"])

    assert result.exit_code != 0
    assert "dashboard open" in result.output
    assert "dashboard share list" in result.output


def test_dashboard_help_shows_canonical_subcommands_only():
    runner = CliRunner()

    result = runner.invoke(cmd_dashboard.dashboard, ["--help"])

    assert result.exit_code == 0, result.output
    assert "open" in result.output
    assert "share" in result.output
    assert "--agent" not in result.output


def test_dashboard_direct_invocation_resets_output_mode_after_json(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    json_result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-test", "--output", "json"])
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["ok"] is True

    pretty_result = runner.invoke(cmd_dashboard.dashboard, ["ar-test"])
    assert pretty_result.exit_code == 0, pretty_result.output
    assert not pretty_result.output.lstrip().startswith("{")
    assert "Dashboard 打开结果" in pretty_result.output
