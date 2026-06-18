from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

from click.testing import CliRunner

from ksadk.cli.cmd_completion import completion


def test_completion_bash_script_strips_click_typed_prefix():
    runner = CliRunner()
    result = runner.invoke(completion, ["bash"])

    assert result.exit_code == 0, result.output
    assert 'line="${line#*,}"' in result.output
    assert "_AGENTENGINE_COMPLETE=bash_complete" in result.output


def test_completion_install_rewrites_zshrc_to_source_after_compinit(tmp_path: Path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))

    zshrc = home / ".zshrc"
    zshrc.write_text(
        """
if command -v agentengine >/dev/null 2>&1; then
  eval "$(_AGENTENGINE_COMPLETE=zsh_source agentengine)"
fi

source /tmp/placeholder
source /Users/test/.agentengine-complete.zsh

autoload -Uz compinit && compinit
""".lstrip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="#compdef agentengine\n", returncode=0),
    )

    runner = CliRunner()
    result = runner.invoke(completion, ["install", "--shell", "zsh"])

    assert result.exit_code == 0, result.output

    expected_source = f'source "{home / ".agentengine-complete.zsh"}"'
    updated = zshrc.read_text(encoding="utf-8")

    assert 'eval "$(_AGENTENGINE_COMPLETE=zsh_source agentengine)"' not in updated
    assert updated.count(expected_source) == 1
    assert updated.rfind("compinit") < updated.rfind(expected_source)


def test_completion_install_prefers_bash_profile_on_macos(tmp_path: Path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)

    bash_profile = home / ".bash_profile"
    bash_profile.write_text("# existing profile\n", encoding="utf-8")

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="_agentengine_completion() { :; }\n", returncode=0),
    )

    runner = CliRunner()
    result = runner.invoke(completion, ["install", "--shell", "auto"])

    assert result.exit_code == 0, result.output
    updated = bash_profile.read_text(encoding="utf-8")
    assert f'source "{home / ".agentengine-complete.bash"}"' in updated


def test_completion_install_auto_detects_git_bash_without_shell_env(tmp_path: Path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setenv("MSYSTEM", "MINGW64")

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="_agentengine_completion() { :; }\n", returncode=0),
    )

    runner = CliRunner()
    result = runner.invoke(completion, ["install", "--shell", "auto"])

    assert result.exit_code == 0, result.output
    bashrc = home / ".bashrc"
    assert bashrc.exists()
    assert f'source "{home / ".agentengine-complete.bash"}"' in bashrc.read_text(encoding="utf-8")


def test_completion_install_auto_detects_wsl_without_shell_env(tmp_path: Path, monkeypatch):
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="_agentengine_completion() { :; }\n", returncode=0),
    )

    runner = CliRunner()
    result = runner.invoke(completion, ["install", "--shell", "auto"])

    assert result.exit_code == 0, result.output
    bashrc = home / ".bashrc"
    assert bashrc.exists()
    assert f'source "{home / ".agentengine-complete.bash"}"' in bashrc.read_text(encoding="utf-8")
