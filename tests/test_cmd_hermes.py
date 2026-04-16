import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from ksadk.api.client import DryRunExit
from ksadk.cli import cmd_hermes
from ksadk.cli.ui import OUTPUT_MODE_PRETTY, configure_ui_runtime, status_rich_style


class _FakeHermesClient:
    create_payload = None
    update_payload = None
    updated_agent_id = None
    deleted = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_agent(self, payload):
        self.__class__.create_payload = payload
        return {
            "agent_id": "ar-hermes-1",
            "name": payload["name"],
            "endpoint": "https://hermes.example.com",
            "api_key": "ak-hermes",
        }

    async def update_agent(self, agent_id, payload):
        self.__class__.updated_agent_id = agent_id
        self.__class__.update_payload = payload
        return {
            "agent_id": agent_id,
            "name": "demo-hermes",
            "endpoint": "https://hermes.example.com",
        }

    async def list_agents(self, **kwargs):
        assert kwargs["framework"] == "hermes"
        return {
            "agents": [
                {
                    "agent_id": "ar-hermes-1",
                    "name": "demo-hermes",
                    "status": "RUNNING",
                    "endpoint": "https://hermes.example.com",
                    "region": kwargs["region"],
                }
            ],
            "total": 1,
        }

    async def get_client_bootstrap_config(self, **kwargs):
        assert kwargs["product"] == "hermes"
        assert kwargs["framework"] == "hermes"
        return {"configs": {}}

    async def get_agent(self, agent_id=None, name=None, include_api_key=False):
        return {
            "basic": {
                "agent_id": agent_id or "ar-hermes-1",
                "name": name or "demo-hermes",
                "status": "RUNNING",
                "framework": "hermes",
                "region": "cn-beijing-6",
            },
            "quick_access": {
                "public_endpoint": "https://hermes.example.com",
                "api_key": "ak-hermes" if include_api_key else None,
            },
        }

    async def delete_agent(self, agent_id):
        self.__class__.deleted.append(agent_id)
        return True


class _FakeHermesOrderClient(_FakeHermesClient):
    get_agent_calls = 0

    async def create_agent(self, payload):
        self.__class__.create_payload = payload
        return {"order_id": "order-hermes-1"}

    async def get_agent(self, agent_id=None, name=None, include_api_key=False):
        self.__class__.get_agent_calls += 1
        return {
            "basic": {
                "agent_id": "ar-hermes-from-order",
                "name": name or "demo-hermes",
                "status": "RUNNING",
                "framework": "hermes",
                "region": "cn-beijing-6",
            },
            "quick_access": {
                "public_endpoint": "https://order-hermes.example.com",
                "api_key": "ak-order-hermes",
            },
        }


class _FakeNonHermesClient(_FakeHermesClient):
    async def get_agent(self, agent_id=None, name=None, include_api_key=False):
        return {
            "basic": {
                "agent_id": agent_id or "ar-langgraph-1",
                "name": name or "demo-langgraph",
                "status": "RUNNING",
                "framework": "langgraph",
            },
            "quick_access": {
                "public_endpoint": "https://langgraph.example.com",
            },
        }


class _FakeHermesDryRunClient(_FakeHermesClient):
    async def create_agent(self, payload):
        raise DryRunExit(
            "dry-run",
            payload={
                "method": "POST",
                "url": "http://example.com/?Action=CreateAgentProduct&Version=2024-06-12",
                "headers": {
                    "Authorization": "Bearer sk-live-secret",
                    "Content-Type": "application/json",
                },
                "body": {
                    "Advanced": {
                        "EnvironmentVariables": [
                            {"Key": "OPENAI_API_KEY", "Value": "sk-test-secret", "IsSensitive": True},
                            {"Key": "OPENAI_MODEL_NAME", "Value": "glm-test", "IsSensitive": False},
                        ]
                    }
                },
                "curl": """curl -X POST "http://example.com" \\
  -H "Authorization: Bearer sk-live-secret" \\
  -d '{"Advanced":{"EnvironmentVariables":[{"Key":"OPENAI_API_KEY","Value":"sk-test-secret","IsSensitive":true}]}}'""",
            },
        )


class _FakeHermesBootstrapImageClient(_FakeHermesClient):
    async def get_client_bootstrap_config(self, **kwargs):
        assert kwargs["product"] == "hermes"
        assert kwargs["framework"] == "hermes"
        return {
            "configs": {
                "bootstrap.default_image": "registry.example.com/agentengine-public/hermes-agent:db-meta"
            }
        }


def test_hermes_exec_accepts_readonly_subcommand_and_uses_remote_terminal(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def _fake_exec(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_exec)
    monkeypatch.setattr(cmd_hermes, "_resolve_hermes_access", lambda **_kwargs: {
        "endpoint": "https://hermes.example.com",
        "api_key": "ak-hermes",
    })

    result = runner.invoke(cmd_hermes.hermes, ["exec", "ar-hermes-1", "--", "status"])

    assert result.exit_code == 0, result.output
    assert captured["endpoint"] == "https://hermes.example.com"
    assert captured["api_key"] == "ak-hermes"
    assert captured["mode"] == "exec"
    assert captured["argv"] == ["status"]


def test_hermes_exec_rejects_mutating_subcommand_before_remote_call(monkeypatch):
    runner = CliRunner()

    async def _forbidden_exec(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _forbidden_exec)

    result = runner.invoke(cmd_hermes.hermes, ["exec", "ar-hermes-1", "--", "gateway", "restart"])

    assert result.exit_code != 0
    assert "不允许" in result.output or "not allowed" in result.output


def test_hermes_exec_exits_cleanly_on_keyboard_interrupt(monkeypatch):
    runner = CliRunner()

    def _fake_exec(**_kwargs):
        return object()

    def _raise_keyboard_interrupt(_awaitable):
        raise KeyboardInterrupt

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_exec)
    monkeypatch.setattr(cmd_hermes, "_resolve_hermes_access", lambda **_kwargs: {
        "endpoint": "https://hermes.example.com",
        "api_key": "ak-hermes",
    })
    monkeypatch.setattr(cmd_hermes.asyncio, "run", _raise_keyboard_interrupt)

    result = runner.invoke(cmd_hermes.hermes, ["exec", "ar-hermes-1", "--", "status"])

    assert result.exit_code == 130
    assert "Traceback" not in result.output


def test_hermes_pairing_accepts_safe_subcommand_and_uses_remote_terminal(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def _fake_pairing(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_pairing)
    monkeypatch.setattr(
        cmd_hermes,
        "_resolve_hermes_access",
        lambda **_kwargs: {
            "endpoint": "https://hermes.example.com",
            "api_key": "ak-hermes",
        },
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["pairing", "ar-hermes-1", "--", "approve", "feishu", "ABC123"],
    )

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "pairing"
    assert captured["argv"] == ["approve", "feishu", "ABC123"]


def test_hermes_pairing_without_agent_ref_uses_state_resolution(monkeypatch):
    runner = CliRunner()
    captured = {}
    resolved = {}

    async def _fake_pairing(**kwargs):
        captured.update(kwargs)

    def _fake_resolve(**kwargs):
        resolved.update(kwargs)
        return {
            "endpoint": "https://hermes.example.com",
            "api_key": "ak-hermes",
        }

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_pairing)
    monkeypatch.setattr(cmd_hermes, "_resolve_hermes_access", _fake_resolve)

    result = runner.invoke(
        cmd_hermes.hermes,
        ["pairing", "--", "approve", "feishu", "ABC123"],
    )

    assert result.exit_code == 0, result.output
    assert resolved["agent_ref"] is None
    assert captured["mode"] == "pairing"
    assert captured["argv"] == ["approve", "feishu", "ABC123"]


def test_hermes_pairing_rejects_invalid_platform_before_remote_call(monkeypatch):
    runner = CliRunner()

    async def _forbidden_pairing(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _forbidden_pairing)

    result = runner.invoke(
        cmd_hermes.hermes,
        ["pairing", "ar-hermes-1", "--", "approve", "unknown-platform", "ABC123"],
    )

    assert result.exit_code != 0
    assert "不允许" in result.output or "not allowed" in result.output


def test_hermes_exec_without_agent_ref_uses_state_resolution(monkeypatch):
    runner = CliRunner()
    captured = {}
    resolved = {}

    async def _fake_exec(**kwargs):
        captured.update(kwargs)

    def _fake_resolve(**kwargs):
        resolved.update(kwargs)
        return {
            "endpoint": "https://hermes.example.com",
            "api_key": "ak-hermes",
        }

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_exec)
    monkeypatch.setattr(cmd_hermes, "_resolve_hermes_access", _fake_resolve)

    result = runner.invoke(
        cmd_hermes.hermes,
        ["exec", "--", "status"],
    )

    assert result.exit_code == 0, result.output
    assert resolved["agent_ref"] is None
    assert captured["mode"] == "exec"
    assert captured["argv"] == ["status"]


def test_hermes_connect_enters_remote_gateway_setup(monkeypatch):
    runner = CliRunner()
    captured = {}

    async def _fake_connect(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _fake_connect)
    monkeypatch.setattr(
        cmd_hermes,
        "_resolve_hermes_access",
        lambda **_kwargs: {
            "endpoint": "https://hermes.example.com",
            "api_key": "ak-hermes",
        },
    )

    result = runner.invoke(cmd_hermes.hermes, ["connect", "ar-hermes-1"])

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "connect"
    assert captured["endpoint"] == "https://hermes.example.com"
    assert captured["api_key"] == "ak-hermes"


def test_hermes_exec_dry_run_does_not_resolve_or_connect(monkeypatch):
    runner = CliRunner()

    async def _forbidden_exec(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _forbidden_exec)
    monkeypatch.setattr(
        cmd_hermes,
        "_resolve_hermes_access",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agent access should not be resolved")),
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["exec", "ar-hermes-1", "--dry-run", "--output", "json", "--", "status"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "hermes"
    assert payload["action"] == "exec"
    assert payload["request"]["argv"] == ["status"]


def test_hermes_connect_dry_run_does_not_resolve_or_connect(monkeypatch):
    runner = CliRunner()

    async def _forbidden_connect(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _forbidden_connect)
    monkeypatch.setattr(
        cmd_hermes,
        "_resolve_hermes_access",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agent access should not be resolved")),
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["connect", "ar-hermes-1", "--dry-run", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "hermes"
    assert payload["action"] == "connect"
    assert payload["request"]["mode"] == "connect"


def test_hermes_pairing_dry_run_does_not_resolve_or_connect(monkeypatch):
    runner = CliRunner()

    async def _forbidden_pairing(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_hermes, "run_hermes_terminal_session", _forbidden_pairing)
    monkeypatch.setattr(
        cmd_hermes,
        "_resolve_hermes_access",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agent access should not be resolved")),
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["pairing", "ar-hermes-1", "--dry-run", "--output", "json", "--", "approve", "feishu", "ABC123"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "hermes"
    assert payload["action"] == "pairing"
    assert payload["request"]["argv"] == ["approve", "feishu", "ABC123"]


def test_hermes_open_defaults_to_manage_and_supports_chat_override(monkeypatch):
    runner = CliRunner()
    opened = []

    monkeypatch.setattr(
        cmd_hermes,
        "_get_hermes_detail",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result={
                "agent_id": "ar-hermes-1",
                "framework": "hermes",
            },
        ),
    )
    monkeypatch.setattr(
        cmd_hermes,
        "_open_dashboard",
        lambda **kwargs: opened.append(kwargs),
    )

    manage_result = runner.invoke(cmd_hermes.hermes, ["open", "ar-hermes-1", "--manage", "--no-open"])
    chat_result = runner.invoke(cmd_hermes.hermes, ["open", "ar-hermes-1", "--chat", "--no-open"])

    assert manage_result.exit_code == 0, manage_result.output
    assert chat_result.exit_code == 0, chat_result.output
    assert opened[0]["ui_path"] == "/"
    assert opened[1]["ui_path"] == "/chat"


def test_hermes_open_force_new_forwards_to_dashboard(monkeypatch):
    runner = CliRunner()
    opened = []

    monkeypatch.setattr(
        cmd_hermes,
        "_get_hermes_detail",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result={
                "agent_id": "ar-hermes-1",
                "framework": "hermes",
            },
        ),
    )
    monkeypatch.setattr(
        cmd_hermes,
        "_open_dashboard",
        lambda **kwargs: opened.append(kwargs),
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["open", "ar-hermes-1", "--chat", "--share", "--expires-seconds", "86400", "--force-new", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert opened[0]["ui_path"] == "/chat"
    assert opened[0]["share"] is True
    assert opened[0]["expires_seconds"] == 86400
    assert opened[0]["force_new"] is True


def test_hermes_open_dry_run_does_not_resolve_or_open(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        cmd_hermes,
        "_get_hermes_detail",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent detail should not be resolved")),
    )
    monkeypatch.setattr(
        cmd_hermes,
        "_open_dashboard",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("dashboard should not open")),
    )

    result = runner.invoke(
        cmd_hermes.hermes,
        ["open", "ar-hermes-1", "--chat", "--dry-run", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["action"] == "open"
    assert payload["request"]["path"] == "/chat"


def test_hermes_open_rejects_manage_and_chat_together(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(
        cmd_hermes,
        "_get_hermes_detail",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result={
                "agent_id": "ar-hermes-1",
                "framework": "hermes",
            },
        ),
    )

    result = runner.invoke(cmd_hermes.hermes, ["open", "ar-hermes-1", "--manage", "--chat", "--no-open"])

    assert result.exit_code != 0


def test_hermes_deploy_creates_container_framework_and_persists_state(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    _FakeHermesClient.update_payload = None
    _FakeHermesClient.updated_agent_id = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes", "--image", "registry/hermes:test"])

    assert result.exit_code == 0, result.output
    assert _FakeHermesClient.create_payload["framework"] == "hermes"
    assert _FakeHermesClient.create_payload["artifact_type"] == "Container"
    assert _FakeHermesClient.create_payload["artifact_path"] == "registry/hermes:test"
    assert _FakeHermesClient.create_payload["ui_config"] == {"profile": "hermes", "path": "/", "url": None}
    assert any(item["Key"] == "OPENAI_API_KEY" and item["Value"] == "sk-test" for item in _FakeHermesClient.create_payload["env_vars"])
    assert "agent_id: ar-hermes-1" in (tmp_path / ".agentengine.state").read_text(encoding="utf-8")


def test_hermes_deploy_defaults_model_base_url_and_omits_api_key(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_hermes, "_get_hermes_global_env", lambda: {}, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy"])

    assert result.exit_code == 0, result.output
    assert "https://kspmas.ksyun.com/v1/" in result.output
    assert "glm-5.1" in result.output
    assert any(
        item["Key"] == "OPENAI_BASE_URL" and item["Value"] == "http://kspmas-internal.sdns.ksyun.com/v1"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )
    assert any(
        item["Key"] == "OPENAI_MODEL_NAME" and item["Value"] == "glm-5.1"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )
    assert not any(item["Key"] == "OPENAI_API_KEY" for item in _FakeHermesClient.create_payload["env_vars"])


def test_hermes_deploy_reads_model_config_from_global_settings(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cmd_hermes,
        "_get_hermes_global_env",
        lambda: {
            "OPENAI_API_KEY": "sk-global",
            "OPENAI_BASE_URL": "https://model.example.com/v1",
            "OPENAI_MODEL_NAME": "glm-global",
        },
        raising=False,
    )
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes", "--image", "registry/hermes:test"])

    assert result.exit_code == 0, result.output
    assert any(item["Key"] == "OPENAI_API_KEY" and item["Value"] == "sk-global" for item in _FakeHermesClient.create_payload["env_vars"])
    assert any(item["Key"] == "OPENAI_BASE_URL" and item["Value"] == "https://model.example.com/v1" for item in _FakeHermesClient.create_payload["env_vars"])
    assert any(item["Key"] == "OPENAI_MODEL_NAME" and item["Value"] == "glm-global" for item in _FakeHermesClient.create_payload["env_vars"])


def test_hermes_deploy_defaults_kspmas_base_url_when_missing(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_hermes, "_get_hermes_global_env", lambda: {}, raising=False)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes"])

    assert result.exit_code == 0, result.output
    assert "https://kspmas.ksyun.com/v1/" in result.output
    assert any(
        item["Key"] == "OPENAI_BASE_URL" and item["Value"] == "http://kspmas-internal.sdns.ksyun.com/v1"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )


def test_hermes_deploy_output_json_emits_result_envelope(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(
        cmd_hermes.hermes,
        ["deploy", "--name", "demo-hermes", "--image", "registry/hermes:test", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "result"
    assert payload["resource"] == "hermes"
    assert payload["action"] == "deploy"
    assert payload["result"]["id"] == "ar-hermes-1"
    assert payload["result"]["image"] == "registry/hermes:test"
    assert payload["result"]["framework"] == "hermes"
    assert payload["result"]["endpoint"] == "https://hermes.example.com"


def test_hermes_deploy_rewrites_public_kspmas_url_for_runtime(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes"])

    assert result.exit_code == 0, result.output
    assert (
        _FakeHermesClient.create_payload["artifact_path"]
        == "hub.kce.ksyun.com/agentengine-public/hermes-agent:v2026.4.13-ks16"
    )
    assert any(
        item["Key"] == "OPENAI_BASE_URL" and item["Value"] == "http://kspmas-internal.sdns.ksyun.com/v1"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )


def test_hermes_deploy_sets_glm_51_context_length_by_default(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes"])

    assert result.exit_code == 0, result.output
    assert any(
        item["Key"] == "HERMES_CONTEXT_LENGTH" and item["Value"] == "200000"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )
    assert any(
        item["Key"] == "HERMES_FALLBACK_MODEL" and item["Value"] == "kimi-k2.5"
        for item in _FakeHermesClient.create_payload["env_vars"]
    )


def test_hermes_deploy_prefers_bootstrap_default_image(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesBootstrapImageClient.create_payload = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesBootstrapImageClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes"])

    assert result.exit_code == 0, result.output
    assert (
        _FakeHermesBootstrapImageClient.create_payload["artifact_path"]
        == "registry.example.com/agentengine-public/hermes-agent:db-meta"
    )


def test_hermes_deploy_updates_existing_hermes_state(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.create_payload = None
    _FakeHermesClient.update_payload = None
    _FakeHermesClient.updated_agent_id = None
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agentengine.state").write_text(
        "type: hermes\nframework: hermes\nagent_id: ar-hermes-existing\nname: demo-hermes\nendpoint: https://old.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--image", "registry/hermes:new"])

    assert result.exit_code == 0, result.output
    assert _FakeHermesClient.create_payload is None
    assert _FakeHermesClient.updated_agent_id == "ar-hermes-existing"
    assert _FakeHermesClient.update_payload["framework"] == "hermes"
    assert _FakeHermesClient.update_payload["artifact_type"] == "Container"
    assert _FakeHermesClient.update_payload["artifact_path"] == "registry/hermes:new"


def test_hermes_deploy_dry_run_redacts_sensitive_values(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesDryRunClient)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "sk-test-secret" not in result.output
    assert "sk-live-secret" not in result.output
    assert "***" in result.output
    assert "glm-test" in result.output


def test_hermes_deploy_polls_order_until_agent_access_is_available(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    _FakeHermesOrderClient.get_agent_calls = 0
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-test")
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesOrderClient)

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cmd_hermes.asyncio, "sleep", _fake_sleep)

    result = runner.invoke(cmd_hermes.hermes, ["deploy", "--name", "demo-hermes", "--image", "registry/hermes:test"])

    assert result.exit_code == 0, result.output
    assert _FakeHermesOrderClient.get_agent_calls == 1
    state = (tmp_path / ".agentengine.state").read_text(encoding="utf-8")
    assert "agent_id: ar-hermes-from-order" in state
    assert "endpoint: https://order-hermes.example.com" in state
    assert "api_key: ak-order-hermes" in state


def test_hermes_list_status_and_delete_use_hermes_resource(monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.deleted = []
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)
    monkeypatch.setattr(cmd_hermes, "confirm_destructive", lambda **_kwargs: True)

    list_result = runner.invoke(cmd_hermes.hermes, ["list"])
    status_result = runner.invoke(cmd_hermes.hermes, ["status", "ar-hermes-1"])
    delete_result = runner.invoke(cmd_hermes.hermes, ["delete", "ar-hermes-1", "-y"])

    assert list_result.exit_code == 0, list_result.output
    assert status_result.exit_code == 0, status_result.output
    assert delete_result.exit_code == 0, delete_result.output
    assert "ar-hermes-1" in list_result.output
    assert "RUNNING" in status_result.output
    assert _FakeHermesClient.deleted == ["ar-hermes-1"]


def test_hermes_status_passes_status_style_to_descriptor(monkeypatch):
    runner = CliRunner()
    configure_ui_runtime(output_mode=OUTPUT_MODE_PRETTY, no_color=False, stdout_is_tty=True)
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)
    captured = {}

    def _fake_render_descriptor_status(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cmd_hermes, "render_descriptor_status", _fake_render_descriptor_status)

    result = runner.invoke(cmd_hermes.hermes, ["status", "ar-hermes-1"])

    assert result.exit_code == 0, result.output
    assert captured["fields"][1] == ("状态", "RUNNING", status_rich_style("RUNNING"))


def test_hermes_delete_uses_delete_specific_next_steps(monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.deleted = []
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)
    monkeypatch.setattr(cmd_hermes, "confirm_destructive", lambda **_kwargs: True)

    result = runner.invoke(cmd_hermes.hermes, ["delete", "ar-hermes-1", "-y"])

    assert result.exit_code == 0, result.output
    assert "agentengine hermes list" in result.output
    assert "agentengine hermes deploy" in result.output
    assert "agentengine hermes connect" not in result.output
    assert "agentengine hermes pairing" not in result.output


def test_hermes_delete_passes_result_styles_to_descriptor(monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.deleted = []
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)
    monkeypatch.setattr(cmd_hermes, "confirm_destructive", lambda **_kwargs: True)
    captured = {}

    def _fake_render_descriptor_status(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cmd_hermes, "render_descriptor_status", _fake_render_descriptor_status)

    result = runner.invoke(cmd_hermes.hermes, ["delete", "ar-hermes-1", "-y"])

    assert result.exit_code == 0, result.output
    assert captured["fields"][1] == ("已删除", "ar-hermes-1", "ok")
    assert captured["fields"][2] == ("失败", "-", "muted")


def test_hermes_delete_resolves_name_to_agent_id_and_rejects_non_hermes(monkeypatch):
    runner = CliRunner()
    _FakeHermesClient.deleted = []
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeHermesClient)
    monkeypatch.setattr(cmd_hermes, "confirm_destructive", lambda **_kwargs: True)

    delete_by_name = runner.invoke(cmd_hermes.hermes, ["delete", "demo-hermes", "-y"])

    assert delete_by_name.exit_code == 0, delete_by_name.output
    assert _FakeHermesClient.deleted == ["ar-hermes-1"]

    _FakeHermesClient.deleted = []
    monkeypatch.setattr(cmd_hermes, "AgentEngineClient", _FakeNonHermesClient)

    non_hermes = runner.invoke(cmd_hermes.hermes, ["delete", "ar-langgraph-1", "-y"])

    assert non_hermes.exit_code != 0
    assert _FakeHermesClient.deleted == []
