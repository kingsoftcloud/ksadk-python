import sys

import pytest

from ksadk.cli import main


def test_main_without_args_shows_help_without_error_prefix(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentengine"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "AgentEngine CLI" in captured.out
    assert "错误:" not in captured.out
