from __future__ import annotations

import json

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli
from ksadk.cli.dry_run import effective_dry_run
from ksadk.cli.ui import emit_json, is_color_disabled, is_json_output


def _parse_json(output: str) -> dict:
    return json.loads(output.strip())


def test_global_options_are_accepted_at_root_group_and_command_positions(monkeypatch):
    _register_commands()
    runner = CliRunner()

    def fake_run_status_command(*, dry_run: bool, **kwargs):  # noqa: ARG001
        emit_json(
            {
                "json": is_json_output(),
                "dry_run": effective_dry_run(dry_run),
                "no_color": is_color_disabled(),
            }
        )

    monkeypatch.setattr("ksadk.cli.cmd_agent.run_status_command", fake_run_status_command)

    cases = [
        ["--output", "json", "--dry-run", "--no-color", "agent", "list", "--account-id", "2000003485"],
        ["agent", "--output", "json", "--dry-run", "--no-color", "list", "--account-id", "2000003485"],
        ["agent", "list", "--output", "json", "--dry-run", "--no-color", "--account-id", "2000003485"],
    ]

    for argv in cases:
        result = runner.invoke(cli, argv)
        assert result.exit_code == 0, result.output
        assert _parse_json(result.output) == {
            "json": True,
            "dry_run": True,
            "no_color": True,
        }


def test_group_level_output_option_uses_canonical_cli_error_for_unsupported_command():
    _register_commands()
    runner = CliRunner()

    result = runner.invoke(cli, ["agent", "--output", "json", "invoke"])

    assert result.exit_code == 2, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert "--output json" in payload["error"]["message"]


def test_group_level_dry_run_option_uses_canonical_cli_error_for_unsupported_command():
    _register_commands()
    runner = CliRunner()

    result = runner.invoke(cli, ["agent", "--dry-run", "invoke"])

    assert result.exit_code == 2, result.output
    assert "--dry-run" in result.output


def test_agent_list_passes_framework_filter_to_status_command(monkeypatch):
    _register_commands()
    runner = CliRunner()

    def fake_run_status_command(*, framework: str | None, **kwargs):  # noqa: ARG001
        emit_json({"framework": framework})

    monkeypatch.setattr("ksadk.cli.cmd_agent.run_status_command", fake_run_status_command)

    result = runner.invoke(
        cli,
        [
            "agent",
            "list",
            "--account-id",
            "2000003485",
            "--framework",
            " langgraph, adk ",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _parse_json(result.output) == {"framework": " langgraph, adk "}


def test_agent_list_hides_openclaw_and_hermes_by_default(monkeypatch):
    _register_commands()
    runner = CliRunner()

    class FakeAgentClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_agents(self, **kwargs):
            assert kwargs.get("framework") is None
            return {
                "agents": [
                    {
                        "agent_id": "ar-langgraph",
                        "name": "regular-agent",
                        "status": "RUNNING",
                        "framework": "langgraph",
                    },
                    {
                        "agent_id": "ar-openclaw",
                        "name": "openclaw-agent",
                        "status": "RUNNING",
                        "framework": "openclaw",
                    },
                    {
                        "agent_id": "ar-hermes",
                        "name": "hermes-agent",
                        "status": "RUNNING",
                        "framework": "hermes",
                    },
                ],
                "total": 3,
            }

        async def close(self):
            return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", FakeAgentClient)

    result = runner.invoke(
        cli,
        [
            "agent",
            "list",
            "--account-id",
            "2000003485",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert [item["framework"] for item in payload["items"]] == ["langgraph"]
