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


async def _fake_create_ticket(*_args, **_kwargs):
    return {"ticket": "ticket-demo"}


def test_dashboard_uses_access_link_by_default(monkeypatch):
    opened = {}
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["ar-test"])
    assert result.exit_code == 0, result.output
    assert opened["url"] == "http://demo.example.com/s/lnk-1"


def test_dashboard_legacy_ticket_mode(monkeypatch):
    opened = {}
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_ticket", _fake_create_ticket)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["ar-test", "--legacy-ticket"])
    assert result.exit_code == 0, result.output
    assert opened["url"].startswith("http://demo.example.com/")
    assert "ae_ui_ticket=ticket-demo" in opened["url"]


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
