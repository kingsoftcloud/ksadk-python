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
    assert captured["path"] is None
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


def test_dashboard_open_uses_state_region_when_region_is_not_explicit(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
        "agent_id: ar-test\n"
        "region: pre-online\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KSYUN_REGION", raising=False)
    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {"agent_id": "ar-test", "region": "pre-online"},
    )

    async def _fake_resolve(region, primary_ref, fallback_ref):
        captured["region"] = region
        return await _fake_resolve_agent_detail(region, primary_ref, fallback_ref)

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open"])

    assert result.exit_code == 0, result.output
    assert captured["region"] == "pre-online"


def test_dashboard_open_explicit_region_overrides_state_region(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
        "agent_id: ar-test\n"
        "region: pre-online\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KSYUN_REGION", raising=False)
    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {"agent_id": "ar-test", "region": "pre-online"},
    )

    async def _fake_resolve(region, primary_ref, fallback_ref):
        captured["region"] = region
        return await _fake_resolve_agent_detail(region, primary_ref, fallback_ref)

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "--region", "cn-beijing-6"])

    assert result.exit_code == 0, result.output
    assert captured["region"] == "cn-beijing-6"


def test_dashboard_open_prefers_state_region_over_global_config_injected_region(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
        "agent_id: ar-test\n"
        "region: pre-online\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")
    monkeypatch.setenv("KSADK_GLOBAL_CONFIG_ENV_KEYS", "KSYUN_REGION")
    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {"agent_id": "ar-test", "region": "pre-online"},
    )

    async def _fake_resolve(region, primary_ref, fallback_ref):
        captured["region"] = region
        return await _fake_resolve_agent_detail(region, primary_ref, fallback_ref)

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open"])

    assert result.exit_code == 0, result.output
    assert captured["region"] == "pre-online"


def test_dashboard_open_env_region_overrides_state_region(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
        "agent_id: ar-test\n"
        "region: pre-online\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KSYUN_REGION", "cn-shanghai-3")
    monkeypatch.delenv("KSADK_GLOBAL_CONFIG_ENV_KEYS", raising=False)
    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {"agent_id": "ar-test", "region": "pre-online"},
    )

    async def _fake_resolve(region, primary_ref, fallback_ref):
        captured["region"] = region
        return await _fake_resolve_agent_detail(region, primary_ref, fallback_ref)

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open"])

    assert result.exit_code == 0, result.output
    assert captured["region"] == "cn-shanghai-3"


def test_dashboard_open_rejects_path_with_embedded_option(monkeypatch):
    runner = CliRunner()

    async def _unexpected_resolve(*_args, **_kwargs):
        raise AssertionError("dashboard open should reject malformed --path before remote lookup")

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _unexpected_resolve)

    result = runner.invoke(
        cmd_dashboard.dashboard,
        ["open", "ar-test", "--path", "/chat--share", "--expires-seconds", "3600", "--no-open"],
    )

    assert result.exit_code != 0
    assert "--path 的值疑似拼入了 `--share`" in result.output
    assert "agentengine dashboard open --path /chat --share" in result.output


def test_dashboard_remote_open_uses_hosted_chat_path_even_with_custom_ui_state(monkeypatch):
    runner = CliRunner()
    captured = {}

    monkeypatch.setattr(
        cmd_dashboard,
        "load_state",
        lambda _cwd: {
            "ui_profile": "custom",
            "ui_path": "/custom-chat",
            "ui_url": "https://ui.example.com/custom-chat/",
        },
    )
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)

    async def _fake_create(*_args, **kwargs):
        captured.update(kwargs)
        return await _fake_create_access_link()

    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-test"])

    assert result.exit_code == 0, result.output
    assert captured["path"] is None


def test_dashboard_open_resolves_openclaw_state_from_cwd(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    opened = {}
    captured = {}

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

    class _FakeGateway:
        async def build_access_info(self, *, path="/", expires_seconds=None, link_type="private", force_new=False):
            captured.update(
                {
                    "path": path,
                    "expires_seconds": expires_seconds,
                    "link_type": link_type,
                    "force_new": force_new,
                }
            )
            return type(
                "Info",
                (),
                {
                    "access_url": "http://demo.example.com/s/gateway-1",
                    "ws_url": "ws://demo.example.com/",
                    "link_id": "gateway-1",
                    "expires_at": None,
                },
            )()

        async def close(self):
            return None

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_build_openclaw_gateway_client", lambda _region, _detail: _FakeGateway())
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(cmd_dashboard.dashboard, ["open"])

    assert result.exit_code == 0, result.output
    assert opened == {}
    assert captured == {"path": None, "expires_seconds": None, "link_type": "private", "force_new": False}
    assert "未显式指定 Agent，使用 .agentengine.state 的 agent_id: ar-openclaw-1" in result.output
    assert "http://demo.example.com/s/gateway-1" in result.output


def test_dashboard_open_omits_path_for_hermes_generic_access_link(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def _fake_resolve(_region, primary_ref, fallback_ref):
        return (
            {
                "agent_id": "ar-hermes-1",
                "name": "demo-hermes",
                "framework": "hermes",
                "endpoint": "http://hermes.example.com",
            },
            primary_ref,
            False,
        )

    async def _fake_create(*_args, **kwargs):
        captured.update(kwargs)
        return await _fake_create_access_link()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create)
    monkeypatch.setattr(
        cmd_dashboard,
        "_create_openclaw_gateway_access_link",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Hermes must not use OpenClaw gateway link")),
    )
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-hermes-1"])

    assert result.exit_code == 0, result.output
    assert captured["path"] is None
    assert captured["expires_seconds"] is None


def test_dashboard_open_force_new_passes_through(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def _fake_resolve(_region, primary_ref, fallback_ref):
        return (
            {
                "agent_id": "ar-hermes-1",
                "name": "demo-hermes",
                "framework": "hermes",
                "endpoint": "http://hermes.example.com",
            },
            primary_ref,
            False,
        )

    async def _fake_create(*_args, **kwargs):
        captured.update(kwargs)
        return await _fake_create_access_link()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(
        cmd_dashboard.dashboard,
        ["open", "ar-hermes-1", "--path", "/", "--share", "--expires-seconds", "86400", "--force-new", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert captured["path"] == "/"
    assert captured["link_type"] == "share"
    assert captured["expires_seconds"] == 86400
    assert captured["force_new"] is True


def test_dashboard_open_routes_openclaw_to_gateway_short_link(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    opened = {}
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
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
        return (
            {
                "agent_id": "ar-openclaw-1",
                "name": "demo-openclaw",
                "framework": "-",
                "endpoint": "http://demo.example.com",
            },
            primary_ref,
            False,
        )

    class _FakeGateway:
        async def build_access_info(self, *, path="/", expires_seconds=None, link_type="private", force_new=False):
            captured.update(
                {
                    "path": path,
                    "expires_seconds": expires_seconds,
                    "link_type": link_type,
                    "force_new": force_new,
                }
            )
            return type(
                "Info",
                (),
                {
                    "access_url": "http://demo.example.com/s/gateway-1",
                    "ws_url": "ws://demo.example.com/",
                    "link_id": "gateway-1",
                    "expires_at": None,
                },
            )()

        async def close(self):
            return None

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_build_openclaw_gateway_client", lambda _region, _detail: _FakeGateway())
    monkeypatch.setattr(
        cmd_dashboard,
        "_create_dashboard_access_link",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not create generic dashboard link")),
    )
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened.setdefault("url", url))

    result = runner.invoke(
        cmd_dashboard.dashboard,
        ["--share", "--expires-seconds", "0", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert opened == {}
    assert captured == {"path": None, "expires_seconds": 0, "link_type": "share", "force_new": False}
    assert "http://demo.example.com/s/gateway-1" in result.output


def test_dashboard_open_passes_custom_path_to_openclaw_gateway_link(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
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

    class _FakeGateway:
        async def build_access_info(self, *, path="/", expires_seconds=None, link_type="private", force_new=False):
            captured.update(
                {
                    "path": path,
                    "expires_seconds": expires_seconds,
                    "link_type": link_type,
                    "force_new": force_new,
                }
            )
            return type(
                "Info",
                (),
                {
                    "access_url": "http://demo.example.com/s/gateway-chat",
                    "ws_url": "ws://demo.example.com/",
                    "link_id": "gateway-chat",
                    "expires_at": None,
                },
            )()

        async def close(self):
            return None

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_build_openclaw_gateway_client", lambda _region, _detail: _FakeGateway())

    result = runner.invoke(
        cmd_dashboard.dashboard,
        ["open", "--share", "--path", "/chat", "--expires-seconds", "0", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"path": "/chat", "expires_seconds": 0, "link_type": "share", "force_new": False}


def test_dashboard_open_passes_force_new_to_openclaw_gateway_link(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    captured = {}

    (tmp_path / ".agentengine.state").write_text(
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

    class _FakeGateway:
        async def build_access_info(self, *, path="/", expires_seconds=None, link_type="private", force_new=False):
            captured.update(
                {
                    "path": path,
                    "expires_seconds": expires_seconds,
                    "link_type": link_type,
                    "force_new": force_new,
                }
            )
            return type(
                "Info",
                (),
                {
                    "access_url": "http://demo.example.com/s/gateway-2",
                    "ws_url": "ws://demo.example.com/",
                    "link_id": "gateway-2",
                    "expires_at": None,
                },
            )()

        async def close(self):
            return None

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve)
    monkeypatch.setattr(cmd_dashboard, "_build_openclaw_gateway_client", lambda _region, _detail: _FakeGateway())

    result = runner.invoke(
        cmd_dashboard.dashboard,
        ["open", "--share", "--expires-seconds", "0", "--force-new", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"path": None, "expires_seconds": 0, "link_type": "share", "force_new": True}
    assert "http://demo.example.com/s/gateway-2" in result.output


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


def test_dashboard_open_json_uses_server_returned_link_type(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)

    async def _fake_create_access_link_with_private_type(*_args, **_kwargs):
        return {
            "link_id": "lnk-1",
            "link_type": "private",
            "access_url": "http://demo.example.com/s/lnk-1",
            "expires_at": "2026-03-09T00:00:00Z",
        }

    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_access_link_with_private_type)
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda _url: None)

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-test", "--share", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["type"] == "private"
