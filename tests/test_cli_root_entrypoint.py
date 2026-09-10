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


def test_root_cli_consumes_studio_workspace_config(monkeypatch, tmp_path):
    import os

    from ksadk.studio.configuration import WorkspaceConfiguration
    from ksadk.studio.workspace import Workspace

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "fixture-inherited-ak")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "fixture-inherited-sk")
    monkeypatch.setattr(sys, "argv", ["ksadk", "--help"])
    config = WorkspaceConfiguration(Workspace(tmp_path))
    config.update_settings(
        {"cloudAccessKey": "fixture-workspace-ak", "cloudSecretKey": "fixture-workspace-sk"}
    )
    captured = {}
    monkeypatch.setattr(cli_module, "_register_commands", lambda: None)

    def capture(*args, **kwargs):
        captured["ak"] = os.environ["KSYUN_ACCESS_KEY"]
        captured["sk"] = os.environ["KSYUN_SECRET_KEY"]

    monkeypatch.setattr(cli_module.cli, "main", capture)
    main()
    assert captured == {"ak": "fixture-workspace-ak", "sk": "fixture-workspace-sk"}

    assert ".agentkit/config.yaml" in (tmp_path / ".gitignore").read_text().splitlines()
