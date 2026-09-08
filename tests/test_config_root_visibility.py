from __future__ import annotations

from click.testing import CliRunner

from ksadk.cli import _register_commands, cli


def test_root_help_shows_config_and_completion_but_not_model():
    _register_commands()
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "agentengine config" in result.output
    assert "agentengine completion" in result.output
    assert " model " not in result.output


def test_config_without_subcommand_still_runs_wizard(monkeypatch):
    _register_commands()
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _fake_run_config_wizard(*, config_file: str | None, set_items: tuple, is_global: bool):
        captured["config_file"] = config_file
        captured["set_items"] = set_items
        captured["is_global"] = is_global

    monkeypatch.setattr("ksadk.cli.cmd_config.run_config_wizard", _fake_run_config_wizard)

    result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "config_file": None,
        "set_items": (),
        "is_global": False,
    }


def test_config_requires_interactive_tty_for_wizard_path():
    _register_commands()
    runner = CliRunner()

    result = runner.invoke(cli, ["config"])

    assert result.exit_code == 2, result.output
    assert "需要交互式终端" in result.output
    assert "config show" in result.output
    assert "config set" in result.output
