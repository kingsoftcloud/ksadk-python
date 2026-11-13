from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from ksadk.cli import cmd_config
from ksadk.cli.error_utils import CLIError
from ksadk.configs import global_config

_FAKE_TUNNEL_TOKEN = "fixture-tunnel-token"


class _FakeQuestion:
    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


def _install_wizard_answers(
    monkeypatch,
    tmp_path: Path,
    *,
    framework: str = "adk",
    enable_tunnel: bool = True,
    tunnel_token: str = _FAKE_TUNNEL_TOKEN,
):
    questions: list[tuple[str, str]] = []
    saved_global: list[dict] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_config, "is_stdout_tty", lambda: True)
    monkeypatch.setattr(global_config, "global_config_exists", lambda: True)
    monkeypatch.setattr(global_config, "load_global_config", lambda: {})
    monkeypatch.setattr(
        global_config,
        "save_global_config",
        lambda value: saved_global.append(value) or True,
    )

    def text(message, default="", **_kwargs):
        questions.append(("text", message))
        return _FakeQuestion(default)

    def password(message, default="", **_kwargs):
        questions.append(("password", message))
        if "Tunnel" in message:
            return _FakeQuestion(tunnel_token)
        return _FakeQuestion(default)

    def confirm(message, default=False, **_kwargs):
        questions.append(("confirm", message))
        if "Tunnel" in message:
            return _FakeQuestion(enable_tunnel)
        return _FakeQuestion(False)

    def select(message, choices, default=None, **_kwargs):
        questions.append(("select", message))
        if "开发框架" in message:
            return _FakeQuestion(framework)
        return _FakeQuestion(default if default in choices else choices[0])

    monkeypatch.setattr(cmd_config.questionary, "text", text)
    monkeypatch.setattr(cmd_config.questionary, "password", password)
    monkeypatch.setattr(cmd_config.questionary, "confirm", confirm)
    monkeypatch.setattr(cmd_config.questionary, "select", select)
    return questions, saved_global


def _run_wizard(tmp_path: Path, *, is_global: bool = False) -> None:
    cmd_config.run_config_wizard(
        config_file=str(tmp_path / "agentengine.yaml"),
        set_items=(),
        is_global=is_global,
    )


def _project_env(tmp_path: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in dotenv_values(tmp_path / ".env", encoding="utf-8-sig").items()
        if value is not None
    }


def test_config_set_tunnel_vars_stay_project_scoped_with_global(monkeypatch, tmp_path):
    saved_global: list[dict] = []
    monkeypatch.setattr(global_config, "load_global_config", lambda: {})
    monkeypatch.setattr(global_config, "get_env_from_global_config", lambda: {})
    monkeypatch.setattr(
        global_config,
        "save_global_config",
        lambda value: saved_global.append(value) or True,
    )

    result = cmd_config._apply_set_command(
        (
            "KSADK_ENABLE_TUNNEL=1",
            f"KSADK_TUNNEL_TOKEN={_FAKE_TUNNEL_TOKEN}",
        ),
        tmp_path / "agentengine.yaml",
        tmp_path / ".env",
        True,
    )

    env = _project_env(tmp_path)
    assert env["KSADK_ENABLE_TUNNEL"] == "1"
    assert env["KSADK_TUNNEL_TOKEN"] == _FAKE_TUNNEL_TOKEN
    assert result["global_unsupported_env_keys"] == [
        "KSADK_ENABLE_TUNNEL",
        "KSADK_TUNNEL_TOKEN",
    ]
    assert saved_global
    assert "tunnel" not in saved_global[0]
    assert _FAKE_TUNNEL_TOKEN not in yaml.safe_dump(saved_global[0])

    warnings: list[str] = []
    monkeypatch.setattr(cmd_config, "print_warn", warnings.append)
    cmd_config._render_config_set_result_pretty(result)
    assert any("仅写入当前项目" in message for message in warnings)
    assert _FAKE_TUNNEL_TOKEN not in "\n".join(warnings)


def test_adk_wizard_enables_tunnel_without_self_url(monkeypatch, tmp_path):
    questions, _ = _install_wizard_answers(monkeypatch, tmp_path)

    _run_wizard(tmp_path)

    env = _project_env(tmp_path)
    assert env["KSADK_ENABLE_TUNNEL"] == "1"
    assert env["KSADK_TUNNEL_TOKEN"] == _FAKE_TUNNEL_TOKEN
    assert "KSADK_TUNNEL_SELF_URL" not in env
    assert any(kind == "confirm" and "Tunnel" in message for kind, message in questions)


def test_new_adk_project_declining_tunnel_does_not_add_env(monkeypatch, tmp_path):
    _install_wizard_answers(monkeypatch, tmp_path, enable_tunnel=False)

    _run_wizard(tmp_path)

    env = _project_env(tmp_path)
    assert "KSADK_ENABLE_TUNNEL" not in env
    assert "KSADK_TUNNEL_TOKEN" not in env


def test_adk_wizard_disable_preserves_token_and_self_url(monkeypatch, tmp_path):
    (tmp_path / "agentengine.yaml").write_text(
        "name: fixture_agent\nframework: adk\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "KSADK_ENABLE_TUNNEL=1\n"
        f"KSADK_TUNNEL_TOKEN={_FAKE_TUNNEL_TOKEN}\n"
        "KSADK_TUNNEL_SELF_URL=http://127.0.0.1:8080\n",
        encoding="utf-8",
    )
    _install_wizard_answers(monkeypatch, tmp_path, enable_tunnel=False)

    _run_wizard(tmp_path)

    env = _project_env(tmp_path)
    assert env["KSADK_ENABLE_TUNNEL"] == "0"
    assert env["KSADK_TUNNEL_TOKEN"] == _FAKE_TUNNEL_TOKEN
    assert env["KSADK_TUNNEL_SELF_URL"] == "http://127.0.0.1:8080"


def test_non_adk_wizard_does_not_touch_tunnel_env(monkeypatch, tmp_path):
    (tmp_path / "agentengine.yaml").write_text(
        "name: fixture_agent\nframework: langgraph\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"KSADK_ENABLE_TUNNEL=1\nKSADK_TUNNEL_TOKEN={_FAKE_TUNNEL_TOKEN}\n",
        encoding="utf-8",
    )
    questions, _ = _install_wizard_answers(
        monkeypatch,
        tmp_path,
        framework="langgraph",
    )

    _run_wizard(tmp_path)

    env = _project_env(tmp_path)
    assert env["KSADK_ENABLE_TUNNEL"] == "1"
    assert env["KSADK_TUNNEL_TOKEN"] == _FAKE_TUNNEL_TOKEN
    assert not any("Tunnel" in message for _, message in questions)


def test_adk_wizard_requires_non_empty_tunnel_token(monkeypatch, tmp_path):
    _install_wizard_answers(monkeypatch, tmp_path, tunnel_token="  ")

    with pytest.raises(CLIError, match="KSADK_TUNNEL_TOKEN"):
        _run_wizard(tmp_path)


def test_adk_wizard_warns_that_tunnel_is_not_global(monkeypatch, tmp_path):
    _, saved_global = _install_wizard_answers(monkeypatch, tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(cmd_config, "print_warn", warnings.append)

    _run_wizard(tmp_path, is_global=True)

    assert saved_global
    assert "tunnel" not in saved_global[0]
    assert any("KSADK_ENABLE_TUNNEL" in message for message in warnings)
    assert any("仅写入当前项目" in message for message in warnings)
    assert _FAKE_TUNNEL_TOKEN not in "\n".join(warnings)
