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


def test_deploy_keeps_oauth_bootstrap_inputs_out_of_shell_forwarding():
    settings = {
        "KSADK_MCP_OAUTH_CONFIG_FILE": "/local/lab.json",
        "KSADK_MCP_OAUTH_PROJECTION": "{}",
        "KSADK_MCP_SERVERS": "[]",
        "KSADK_MCP_KEY": "test-key",
    }
    assert forward_shell_process_env({}, settings) == {
        "KSADK_MCP_SERVERS": "[]",
        "KSADK_MCP_KEY": "test-key",
    }


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


def test_local_native_studio_selection_does_not_leak_into_cloud_environment():
    environ = {
        "KSADK_STUDIO_NATIVE_DEFAULT": "1",
        "KSADK_STUDIO_NATIVE_AGENT_IDS": "local-agent",
        "KSADK_STUDIO_NATIVE_DURABLE": "1",
        "KSADK_STUDIO_AGENT_ID": "local-agent",
        "OPENAI_MODEL_NAME": "fixture-model",
    }
    forwarded = forward_shell_process_env({}, environ)

    assert forwarded == {"OPENAI_MODEL_NAME": "fixture-model"}


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


_TUNNEL_ENV = {
    "KSADK_ENABLE_TUNNEL": "1",
    "KSADK_TUNNEL_TOKEN": "fixture-tunnel-token",
    "KSADK_TUNNEL_SELF_URL": "http://127.0.0.1:8080",
}


def _clear_tunnel_process_env(monkeypatch):
    for name in _TUNNEL_ENV:
        monkeypatch.delenv(name, raising=False)


def _disable_global_deploy_env(monkeypatch):
    monkeypatch.setattr(
        "ksadk.deployment.providers.serverless.get_env_from_global_config",
        lambda: {},
    )


def test_tunnel_process_env_prefixes_are_forwarded():
    for name in _TUNNEL_ENV:
        assert should_forward_process_env(name)


def test_tunnel_project_dotenv_enters_deploy_env(monkeypatch, tmp_path):
    from ksadk.deployment.providers.serverless import ServerlessProvider

    _disable_global_deploy_env(monkeypatch)
    _clear_tunnel_process_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in _TUNNEL_ENV.items()),
        encoding="utf-8",
    )

    env, env_file_exists, project_env_count = ServerlessProvider._load_deploy_env_vars(tmp_path)

    assert env_file_exists is True
    assert project_env_count == len(_TUNNEL_ENV)
    for key, value in _TUNNEL_ENV.items():
        assert env[key] == value


def test_tunnel_deploy_env_precedence_and_no_generated_self_url(monkeypatch, tmp_path):
    from ksadk.deployment.providers.serverless import ServerlessProvider

    _disable_global_deploy_env(monkeypatch)
    _clear_tunnel_process_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "KSADK_ENABLE_TUNNEL=0\n"
        "KSADK_TUNNEL_TOKEN=fixture-project-token\n"
        "KSADK_TUNNEL_SELF_URL=http://127.0.0.1:7000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fixture-shell-token")
    monkeypatch.setenv("KSADK_TUNNEL_SELF_URL", "http://127.0.0.1:9000")

    env, _, _ = ServerlessProvider._load_deploy_env_vars(
        tmp_path,
        {"KSADK_TUNNEL_TOKEN": "fixture-explicit-token"},
    )

    assert env["KSADK_ENABLE_TUNNEL"] == "1"
    assert env["KSADK_TUNNEL_TOKEN"] == "fixture-explicit-token"
    assert env["KSADK_TUNNEL_SELF_URL"] == "http://127.0.0.1:9000"

    monkeypatch.delenv("KSADK_TUNNEL_SELF_URL")
    (tmp_path / ".env").write_text(
        "KSADK_ENABLE_TUNNEL=1\nKSADK_TUNNEL_TOKEN=fixture-project-token\n",
        encoding="utf-8",
    )
    env_without_self_url, _, _ = ServerlessProvider._load_deploy_env_vars(tmp_path)
    assert "KSADK_TUNNEL_SELF_URL" not in env_without_self_url


def test_tunnel_env_payload_marks_only_token_as_sensitive():
    from ksadk.api.environment import environment_variables_payload

    payload = environment_variables_payload(_TUNNEL_ENV)
    sensitivity = {item["Key"]: item["IsSensitive"] for item in payload}

    assert sensitivity == {
        "KSADK_ENABLE_TUNNEL": False,
        "KSADK_TUNNEL_TOKEN": True,
        "KSADK_TUNNEL_SELF_URL": False,
    }


def test_tunnel_token_is_masked_in_dry_run_output():
    import json

    from ksadk.api.environment import environment_variables_payload
    from ksadk.cli.dry_run import sanitize_dry_run_request

    payload = sanitize_dry_run_request(
        {
            "method": "POST",
            "url": "https://agentengine.example.com/CreateAgentProduct",
            "body": {
                "Advanced": {
                    "EnvironmentVariables": environment_variables_payload(_TUNNEL_ENV),
                },
            },
        }
    )
    rendered = json.dumps(payload, ensure_ascii=False)

    assert _TUNNEL_ENV["KSADK_TUNNEL_TOKEN"] not in rendered
    env_items = payload["body"]["Advanced"]["EnvironmentVariables"]
    token_item = next(item for item in env_items if item["Key"] == "KSADK_TUNNEL_TOKEN")
    assert token_item["Value"] == "***"


@pytest.mark.asyncio
async def test_tunnel_env_create_and_update_agent_api_payloads(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from ksadk.api.client import AgentEngineClient

    client = AgentEngineClient(
        base_url="https://agentengine.example.com",
        access_key="fixture-access-key",
        secret_key="fixture-secret-key",
        region="cn-beijing-6",
        allow_env_fallback=False,
    )
    create_action = Mock(return_value={})
    monkeypatch.setattr(client, "_action", create_action)

    await client.create_agent(
        {
            "name": "fixture-agent",
            "framework": "adk",
            "artifact_type": "Code",
            "artifact_path": "ks3://fixture-bucket/fixture-agent.zip",
            "region": "cn-beijing-6",
            "env_vars": _TUNNEL_ENV,
        }
    )

    assert create_action.call_count == 1
    action, params = create_action.call_args.args
    assert action == "CreateAgentProduct"
    created_env = params["Advanced"]["EnvironmentVariables"]
    assert {item["Key"] for item in created_env} == set(_TUNNEL_ENV)
    assert all(
        item["Value"] != _TUNNEL_ENV["KSADK_TUNNEL_TOKEN"] or item["IsSensitive"]
        for item in created_env
    )

    update_action = AsyncMock(return_value={})
    monkeypatch.setattr(client, "_action_async", update_action)
    await client.update_agent(
        "fixture-agent-id",
        {
            "artifact_type": "Code",
            "artifact_path": "ks3://fixture-bucket/fixture-agent-v2.zip",
            "env_vars": _TUNNEL_ENV,
            "environment_variables_is_patch": True,
        },
    )

    assert update_action.call_count == 1
    action, params = update_action.call_args.args
    assert action == "UpdateAgent"
    patch = params["EnvironmentVariablePatch"]
    assert patch["Delete"] == []
    assert {item["Key"] for item in patch["Upsert"]} == set(_TUNNEL_ENV)
    token_item = next(item for item in patch["Upsert"] if item["Key"] == "KSADK_TUNNEL_TOKEN")
    assert token_item["IsSensitive"] is True
