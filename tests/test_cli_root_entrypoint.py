import sys

import pytest

import ksadk.cli as cli_module
from ksadk.cli import main


def test_main_without_args_shows_help_without_error_prefix(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentengine"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "AgentEngine CLI" in captured.out
    assert "错误:" not in captured.out


def test_main_keyboard_interrupt_during_command_loading_exits_without_traceback(
    monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["agentengine", "studio"])

    def interrupted_registration() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_register_commands", interrupted_registration)

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 130
    assert "Traceback" not in captured.err
