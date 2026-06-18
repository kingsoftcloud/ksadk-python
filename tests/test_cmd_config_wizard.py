from __future__ import annotations

from pathlib import Path

import yaml

from ksadk.cli import cmd_config


class _Prompt:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def test_config_wizard_accepts_existing_hermes_framework(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_config, "is_stdout_tty", lambda: True)
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: True)

    (tmp_path / "agentengine.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "hermes",
                "description": "existing description",
                "framework": "hermes",
                "entry_point": "hermes/agent.py",
                "agent_variable": "root_agent",
                "region": "pre-online",
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    def _text(_message, *, default="", **_kwargs):
        return _Prompt(default)

    def _password(_message, *, default="", **_kwargs):
        return _Prompt(default)

    def _confirm(message, *, default=False, **_kwargs):
        assert message in {"是否配置金山云凭证?", "是否使用 container 模式部署?"}
        return _Prompt(default)

    def _select(_message, *, choices, default=None, **_kwargs):
        if default not in choices:
            raise ValueError(f"default {default!r} is not a valid choice")
        return _Prompt(default)

    monkeypatch.setattr(cmd_config.questionary, "text", _text)
    monkeypatch.setattr(cmd_config.questionary, "password", _password)
    monkeypatch.setattr(cmd_config.questionary, "confirm", _confirm)
    monkeypatch.setattr(cmd_config.questionary, "select", _select)

    cmd_config.run_config_wizard(config_file=None, set_items=(), is_global=False)

    updated = yaml.safe_load((tmp_path / "agentengine.yaml").read_text(encoding="utf-8-sig"))
    assert updated["framework"] == "hermes"


def test_config_wizard_prompts_for_kcr_username(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_config, "is_stdout_tty", lambda: True)
    monkeypatch.setattr("ksadk.configs.global_config.global_config_exists", lambda: True)

    text_answers = {
        "Agent 名称:": "demo-agent",
        "Agent 描述:": "demo",
        "Base URL (OPENAI_BASE_URL) [选填,默认使用金山云星流平台URL]:": "",
        "模型名称 (OPENAI_MODEL_NAME) [选填,默认使用金山云星流平台glm-5.2]:": "",
        "KCR 用户名 (企业版请填写访问凭证用户名):": "enterprise-user",
        "镜像仓库地址 [选填,如: agenthzzqy-vpc.ksyunkcr.com/testagent-pub]:": "agenthzzqy-vpc.ksyunkcr.com/testagent-pub",
    }
    password_answers = {
        "API Key (OPENAI_API_KEY):": "",
        "KCR 密码或 Token:": "enterprise-pass",
    }

    def _text(message, *, default="", **_kwargs):
        return _Prompt(text_answers.get(message, default))

    def _password(message, *, default="", **_kwargs):
        return _Prompt(password_answers.get(message, default))

    def _confirm(message, *, default=False, **_kwargs):
        if message == "是否配置金山云凭证?":
            return _Prompt(False)
        if message == "是否使用 container 模式部署?":
            return _Prompt(True)
        raise AssertionError(f"unexpected confirm prompt: {message}")

    def _select(_message, *, default=None, **_kwargs):
        return _Prompt(default)

    monkeypatch.setattr(cmd_config.questionary, "text", _text)
    monkeypatch.setattr(cmd_config.questionary, "password", _password)
    monkeypatch.setattr(cmd_config.questionary, "confirm", _confirm)
    monkeypatch.setattr(cmd_config.questionary, "select", _select)

    cmd_config.run_config_wizard(config_file=None, set_items=(), is_global=False)

    env_text = (tmp_path / ".env").read_text(encoding="utf-8-sig")
    assert "KCR_USERNAME=enterprise-user" in env_text
    assert "KCR_PASSWORD=enterprise-pass" in env_text
    assert "KCR_REGISTRY=agenthzzqy-vpc.ksyunkcr.com/testagent-pub" in env_text
