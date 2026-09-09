"""deploy shell 环境变量前缀转发 (hermes/openclaw 对齐通用 deploy) 的测试。"""

import pytest

from ksadk.cli import cmd_hermes, cmd_openclaw
from ksadk.deployment.env_forward import (
    forward_shell_process_env,
    should_forward_process_env,
)


@pytest.fixture(autouse=True)
def _clear_env_caches(monkeypatch):
    monkeypatch.setattr(cmd_hermes, "_HERMES_GLOBAL_ENV_CACHE", None)
    monkeypatch.setattr(cmd_openclaw, "_GLOBAL_ENV_CACHE", None)


def test_should_forward_process_env_prefixes():
    assert should_forward_process_env("KSYUN_ACCESS_KEY")
    assert should_forward_process_env("KSYUN_SECRET_KEY")
    assert should_forward_process_env("KSYUN_ACCOUNT_ID")
    assert should_forward_process_env("OPENAI_API_KEY")
    assert should_forward_process_env("KSADK_SOME_KEY")
    assert should_forward_process_env("E2B_API_KEY")
    assert should_forward_process_env("AGENTENGINE_CLUSTER")
    assert should_forward_process_env("AGENTKIT_MODEL_API_KEY")


def test_should_forward_process_env_rejects_unrelated_and_denylist():
    assert not should_forward_process_env("PATH")
    assert not should_forward_process_env("HOME")
    assert not should_forward_process_env("AWS_ACCESS_KEY_ID")
    # denylist: CLI/builders/configs/web 模块本地键不转发
    assert not should_forward_process_env("KSADK_UPDATED_AT")
    assert not should_forward_process_env("KSADK_VERSION")
    assert not should_forward_process_env("KSADK_GLOBAL_CONFIG_ENV_KEYS")


def test_forward_shell_process_env_setdefault_semantics():
    base = {"OPENAI_BASE_URL": "https://resolved.example.com/v1"}
    environ = {
        "OPENAI_BASE_URL": "https://shell.example.com/v1",
        "KSYUN_ACCESS_KEY": "ak-from-shell",
        "PATH": "/usr/bin",
        "EMPTY_KEY": "",
    }
    forward_shell_process_env(base, environ)

    # 不覆盖已有键
    assert base["OPENAI_BASE_URL"] == "https://resolved.example.com/v1"
    # 补齐符合规则的缺失键
    assert base["KSYUN_ACCESS_KEY"] == "ak-from-shell"
    # 不符合规则/空值不转发
    assert "PATH" not in base
    assert "EMPTY_KEY" not in base


def test_build_hermes_env_vars_forwards_shell_ksyun(monkeypatch):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ak-shell")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "sk-shell")
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")

    env_list = cmd_hermes._build_hermes_env_vars(shell_keys=set())
    env = {item["Key"]: item["Value"] for item in env_list}

    assert env["KSYUN_ACCESS_KEY"] == "ak-shell"
    assert env["KSYUN_SECRET_KEY"] == "sk-shell"
    assert env["KSYUN_REGION"] == "cn-beijing-6"


def test_build_hermes_env_vars_cli_env_overrides_shell_forward(monkeypatch):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ak-shell")

    env_list = cmd_hermes._build_hermes_env_vars(
        cli_env={"KSYUN_ACCESS_KEY": "ak-cli"},
        shell_keys={"KSYUN_ACCESS_KEY"},
    )
    env = {item["Key"]: item["Value"] for item in env_list}

    assert env["KSYUN_ACCESS_KEY"] == "ak-cli"


def test_build_openclaw_env_vars_forwards_shell_ksyun(monkeypatch):
    monkeypatch.setenv("KSYUN_ACCESS_KEY", "ak-shell")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "sk-shell")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert env["KSYUN_ACCESS_KEY"] == "ak-shell"
    assert env["KSYUN_SECRET_KEY"] == "sk-shell"


def test_build_openclaw_env_vars_does_not_forward_unrelated(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-ak")
    monkeypatch.setenv("KSADK_UPDATED_AT", "123")

    env = cmd_openclaw._build_openclaw_env_vars()

    assert "AWS_ACCESS_KEY_ID" not in env
    assert "KSADK_UPDATED_AT" not in env
