import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import yaml
from click.testing import CliRunner

from ksadk.api.client import AgentEngineAPIError, AgentEngineClient, DryRunExit
from ksadk.cli import _register_commands, cli, cmd_mcp
from ksadk.cli.cmd_agent import agent
from ksadk.cli.cmd_destroy import delete as destroy_delete
from ksadk.cli.cmd_destroy import destroy as destroy_cmd
from ksadk.cli.cmd_mcp import mcp
from ksadk.cli import cmd_openclaw
from ksadk.cli.cmd_openclaw import openclaw
from ksadk.cli.cmd_version import version
from ksadk.cli.dry_run import run_async_with_dry_run
from ksadk.deployment.base import DeployTarget
from ksadk.deployment.providers.serverless import ServerlessProvider


class _FakeDryRunClient:
    last_init_kwargs: Dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        _FakeDryRunClient.last_init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_mcps(self, **kwargs):
        raise DryRunExit("dry-run")

    async def get_mcp(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def delete_mcp(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def list_agents(self, **kwargs):
        raise DryRunExit("dry-run")

    async def get_agent(self, **kwargs):
        raise DryRunExit("dry-run")

    async def delete_agent(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def create_mcp(self, request):
        raise DryRunExit("dry-run", payload={"body": request})

    async def list_versions(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def release_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def rollback_version(self, *_args, **_kwargs):
        raise DryRunExit("dry-run")

    async def close(self):
        return None


class _FakeMCPDetectionResult:
    is_valid = True
    entry_point = "mcp_main.py"
    mcp_variable = "mcp"
    tools = ["test_tool"]


class _FakeMCPDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def detect(self):
        return _FakeMCPDetectionResult()


class _FakeOpenClawListClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def list_agents(self, **_kwargs):
        return {
            "agents": [
                {
                    "agent_id": "ar-demo-1",
                    "name": "demo-openclaw",
                    "status": "running",
                    "endpoint": "https://openclaw.example.com",
                    "region": "cn-beijing-6",
                }
            ],
            "total": 145,
        }

    async def close(self):
        return None


class _FakeOpenClawDetailClient:
    last_log_kwargs: Dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_agent(self, **_kwargs):
        return {
            "basic": {
                "agent_id": "ar-demo-1",
                "name": "demo-openclaw",
                "status": "RUNNING",
                "framework": "openclaw",
                "region": "cn-beijing-6",
            },
            "quick_access": {
                "public_endpoint": "https://openclaw.example.com",
            },
            "advanced": {
                "observability_url": "https://trace.example.com/project/aropenclaw1/traces",
            },
        }

    async def get_agent_logs(self, **kwargs):
        _FakeOpenClawDetailClient.last_log_kwargs = kwargs
        return {
            "logs": ["line-1", "line-2"],
            "total": 2,
            "page": 1,
            "page_size": 200,
            "agent_id": "ar-demo-1",
            "instance": kwargs.get("instance"),
            "log_type": "Stdout",
        }

    async def close(self):
        return None


class _FakeOpenClawCreatingDetailClient(_FakeOpenClawDetailClient):
    async def get_agent(self, **_kwargs):
        payload = await super().get_agent(**_kwargs)
        payload["basic"]["status"] = "CREATING"
        return payload


class _FakeOpenClawFailedDetailClient(_FakeOpenClawDetailClient):
    last_repair_kwargs: Dict[str, Any] = {}

    async def get_agent(self, **_kwargs):
        payload = await super().get_agent(**_kwargs)
        payload["basic"]["status"] = "FAILED"
        return payload

    async def run_openclaw_repair(self, agent_id: str, *, repair_action: str = "doctor-fix"):
        self.__class__.last_repair_kwargs = {
            "agent_id": agent_id,
            "repair_action": repair_action,
        }
        return {
            "ok": True,
            "agent_id": agent_id,
            "repair_action": repair_action,
            "status": "succeeded",
            "exit_code": 0,
            "logs": "fixed",
        }


class _FakeGatewayClient:
    applied_configs: list[Dict[str, Any]] = []
    last_wait_kwargs: Dict[str, Any] = {}
    disconnect_waits: list[int] = []

    def __init__(self, *args, **kwargs):
        self.methods = ["channels.status", "config.get", "web.login.start", "web.login.wait"]

    async def build_access_info(self):
        return SimpleNamespace(
            access_url="https://dashboard.example.com/s/lnk-demo",
            ws_url="wss://dashboard.example.com/",
            link_id="lnk-demo",
            expires_at="2026-03-23T12:00:00Z",
        )

    async def connect(self):
        return {"features": {"methods": self.methods}}

    async def close(self):
        return None

    async def wait_for_disconnect(self, *, timeout_ms=5_000):
        self.__class__.disconnect_waits.append(timeout_ms)
        return True

    async def channels_status(self, *, probe=False, timeout_ms=None):
        return {
            "channels": {
                "weixin": {"connected": True, "probe": probe, "timeout_ms": timeout_ms},
                "feishu": {"enabled": True},
                "wps-xiezuo": {"enabled": True},
            }
        }

    async def config_get(self):
        return {
            "hash": "cfg-1",
            "exists": True,
            "config": {
                "plugins": {"entries": {}},
                "channels": {},
            },
        }

    async def web_login_start(self, *, force=False, timeout_ms=None):
        return {
            "qrDataUrl": "https://qr.example.com/weixin-login",
            "sessionKey": "sess-1",
            "message": "scan now",
        }

    async def web_login_wait(self, *, account_id=None, session_key=None, timeout_ms=None):
        self.__class__.last_wait_kwargs = {
            "account_id": account_id,
            "session_key": session_key,
            "timeout_ms": timeout_ms,
        }
        return {"connected": True, "message": "connected"}

    async def config_apply(self, *, config, base_hash, note=None, session_key=None, restart_delay_ms=None):
        self.__class__.applied_configs.append(
            {
                "config": config,
                "base_hash": base_hash,
                "note": note,
                "session_key": session_key,
                "restart_delay_ms": restart_delay_ms,
            }
        )
        return {"ok": True}


class _FakeConfigApplyReloadGatewayClient(_FakeGatewayClient):
    async def config_apply(self, *, config, base_hash, note=None, session_key=None, restart_delay_ms=None):
        await super().config_apply(
            config=config,
            base_hash=base_hash,
            note=note,
            session_key=session_key,
            restart_delay_ms=restart_delay_ms,
        )
        raise cmd_openclaw.OpenClawGatewayError(
            "Gateway websocket receive failed: received 1011 (internal error) Bad Gateway"
        )


class _FakeDoctorGatewayClient(_FakeGatewayClient):
    async def config_get(self):
        return {
            "hash": "cfg-1",
            "exists": True,
            "config": {
                "plugins": {
                    "entries": {
                        "openclaw-weixin": {"enabled": True},
                        "openclaw-lark": {"enabled": True},
                        "wps-xiezuo": {"enabled": True},
                    }
                },
                "skills": {"allowBundled": ["wps365-skill"]},
                "channels": {
                    "feishu": {"enabled": True},
                    "openclaw-weixin": {"accounts": {"default": {"enabled": True}}},
                    "wps-xiezuo": {"enabled": True, "appId": "app-demo", "appSecret": "secret-demo"},
                },
            },
        }


class _FakeDoctorFreshGatewayClient(_FakeGatewayClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.methods = ["channels.status", "config.get"]

    async def channels_status(self, *, probe=False, timeout_ms=None):
        return {
            "channels": {
                "openclaw-weixin": {"configured": False, "probe": probe, "timeout_ms": timeout_ms},
            }
        }

    async def config_get(self):
        return {
            "hash": "cfg-1",
            "exists": True,
            "config": {
                "plugins": {
                    "entries": {
                        "openclaw-weixin": {"enabled": True},
                        "openclaw-lark": {"enabled": True},
                        "wps-xiezuo": {"enabled": True},
                    }
                },
                "skills": {"allowBundled": ["wps365-skill"]},
                "channels": {},
            },
        }


class _FakeDoctorBrokenWeixinGatewayClient(_FakeDoctorFreshGatewayClient):
    async def channels_status(self, *, probe=False, timeout_ms=None):
        return {
            "channels": {
                "openclaw-weixin": {"configured": True, "probe": probe, "timeout_ms": timeout_ms},
            }
        }

    async def config_get(self):
        snapshot = await super().config_get()
        snapshot["config"]["channels"] = {
            "openclaw-weixin": {"accounts": {"default": {"enabled": True}}},
        }
        return snapshot


class _FakeWeixinGatewayWithoutWebLoginClient(_FakeGatewayClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.methods = ["channels.status", "config.get"]

    async def web_login_start(self, *, force=False, timeout_ms=None):
        raise AssertionError("web login RPC should not be called when method discovery is missing")

    async def channels_status(self, *, probe=False, timeout_ms=None):
        return {
            "channels": {
                "openclaw-weixin": {"configured": True, "connected": False, "probe": probe, "timeout_ms": timeout_ms},
            }
        }


class _FakeWeixinGatewayProviderUnavailableClient(_FakeGatewayClient):
    async def web_login_start(self, *, force=False, timeout_ms=None):
        raise cmd_openclaw.OpenClawGatewayRequestError(
            "web login provider is not available",
            code="INVALID_REQUEST",
        )

    async def channels_status(self, *, probe=False, timeout_ms=None):
        return {
            "channels": {
                "openclaw-weixin": {"configured": True, "connected": False, "probe": probe, "timeout_ms": timeout_ms},
            }
        }


class _FakeRestartingWeixinGatewayClient(_FakeGatewayClient):
    async def web_login_start(self, *, force=False, timeout_ms=None):
        if not self.__class__.disconnect_waits:
            raise AssertionError("expected gateway restart wait before weixin login")
        return await super().web_login_start(force=force, timeout_ms=timeout_ms)


class _FakeDeleteProvider:
    def __init__(self):
        self.calls = []

    async def destroy(self, agent_id, deploy_target):
        self.calls.append((agent_id, deploy_target))
        return True


class _FakeBatchDeleteClient:
    deleted_agents = []
    deleted_mcps = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete_agent(self, agent_id):
        self.deleted_agents.append(agent_id)
        return True

    async def delete_mcp(self, mcp_id):
        self.deleted_mcps.append(mcp_id)
        return True

    async def close(self):
        return None


class _FakeOpenClawCreateClient:
    get_agent_calls = 0
    create_payload = None
    update_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_agent(self, _data):
        self.__class__.create_payload = _data
        return {
            "agent_id": "ar-created-1",
            "endpoint": "https://ar-created-1.agent.kspmas.ksyun.com",
            "api_key": "ak-created-1",
        }

    async def update_agent(self, _agent_id, _data):
        self.__class__.update_payload = _data
        return {
            "agent_id": _agent_id,
            "endpoint": "https://ar-existing-1.agent.kspmas.ksyun.com",
            "api_key": "ak-existing-1",
        }

    async def get_agent(self, **_kwargs):
        self.__class__.get_agent_calls += 1
        raise AssertionError("OpenClaw create response already contains complete quick access")

    async def close(self):
        return None


class _FakeOpenClawImmediateAgentIdClient:
    get_agent_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_agent(self, _data):
        return {
            "agent_id": "ar-created-2",
            "endpoint": None,
            "api_key": None,
            "order_id": "ord-created-2",
        }

    async def get_agent(self, **kwargs):
        self.__class__.get_agent_calls += 1
        assert kwargs["agent_id"] == "ar-created-2"
        assert kwargs["include_api_key"] is True
        return {
            "basic": {
                "agent_id": "ar-created-2",
                "name": "demo-openclaw",
                "status": "RUNNING",
                "framework": "openclaw",
                "region": "cn-beijing-6",
            },
            "quick_access": {
                "public_endpoint": "https://fresh-openclaw.example.com",
                "api_key": "ak-fresh-openclaw",
            },
        }

    async def close(self):
        return None


class _FakeOpenClawDelayedAccessClient:
    get_agent_calls = 0
    suppression_used = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @contextmanager
    def suppress_http_error_logging(self, predicate=None):
        self.__class__.suppression_used = predicate is not None
        yield

    async def create_agent(self, _data):
        return {
            "agent_id": "ar-created-delayed",
            "endpoint": "https://created-openclaw.example.com",
            "api_key": None,
            "order_id": "ord-created-delayed",
        }

    async def get_agent(self, **kwargs):
        self.__class__.get_agent_calls += 1
        if self.__class__.get_agent_calls < 4:
            raise AgentEngineAPIError(
                404,
                "未找到对应的 Agent",
                details={
                    "http_status": 404,
                    "remote_error_message": "未找到对应的 Agent",
                },
            )
        assert kwargs["agent_id"] == "ar-created-delayed"
        assert kwargs["include_api_key"] is True
        return {
            "basic": {
                "agent_id": "ar-created-delayed",
                "name": "demo-openclaw",
                "status": "RUNNING",
                "framework": "openclaw",
                "region": "cn-beijing-6",
            },
            "quick_access": {
                "public_endpoint": "https://ready-openclaw.example.com",
                "api_key": "ak-ready-openclaw",
            },
            "deployment": {
                "framework": "openclaw",
                "region": "cn-beijing-6",
            },
        }

    async def close(self):
        return None


class _FakeDeleteClient:
    deleted_agents = []
    should_succeed = True

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def delete_agent(self, agent_id):
        self.deleted_agents.append(agent_id)
        if self.should_succeed:
            return True
        raise RuntimeError("delete failed")

    async def close(self):
        return None


class _FakePartialDeleteProvider:
    def __init__(self, results: Dict[str, bool]):
        self.results = dict(results)
        self.calls = []

    async def destroy(self, agent_id, deploy_target):
        self.calls.append((agent_id, deploy_target))
        return self.results.get(agent_id, False)


def test_run_async_with_dry_run_handles_exit(capsys):
    async def _boom():
        raise DryRunExit("done")

    result = run_async_with_dry_run(_boom(), dry_run=True)
    assert result is None
    out = capsys.readouterr().out
    assert "Dry Run Completed" in out


def test_client_respects_global_dry_run_env(monkeypatch):
    monkeypatch.setenv("AGENTENGINE_GLOBAL_DRY_RUN", "1")
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="", dry_run=False)
    assert client.dry_run is True


def test_mcp_status_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        mcp,
        ["status", "mcp-123", "--dry-run"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_mcp_deploy_dry_run_json_plan(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    def _should_not_build(*_args, **_kwargs):
        raise AssertionError("Dry run should not build artifacts")

    monkeypatch.setattr(cmd_mcp, "_build_code_artifact", _should_not_build)

    result = runner.invoke(
        mcp,
        ["deploy", ".", "--dry-run", "--output", "json", "--ks3-bucket", "agentengine-test"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resource"] == "workflow"
    assert payload["action"] == "deploy"
    assert payload["kind"] == "dry_run"
    assert payload["request"]["body"]["artifact_type"] == "Code"
    assert payload["plan"]["artifact"]["reference"].startswith("ks3://agentengine-test/")


def test_mcp_deploy_dry_run_includes_explicit_network(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    def _should_not_build(*_args, **_kwargs):
        raise AssertionError("Dry run should not build artifacts")

    monkeypatch.setattr(cmd_mcp, "_build_code_artifact", _should_not_build)

    result = runner.invoke(
        mcp,
        [
            "deploy",
            ".",
            "--dry-run",
            "--output",
            "json",
            "--ks3-bucket",
            "agentengine-test",
            "--disable-public-access",
            "--enable-vpc-access",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
            "--availability-zone",
            "cn-beijing-6b",
        ],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["request"]["body"]["network"] == {
        "enable_public_access": False,
        "enable_vpc_access": True,
        "vpc_id": "vpc-cli",
        "subnet_id": "subnet-cli",
        "security_group_id": "sg-cli",
        "availability_zone": "cn-beijing-6b",
    }


def test_openclaw_list_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(openclaw, ["list", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_openclaw_list_shows_account_region_summary(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawListClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})

    result = runner.invoke(
        openclaw,
        ["list", "--region", "cn-beijing-6"],
        env={"KSYUN_ACCOUNT_ID": "2000003485"},
    )

    assert result.exit_code == 0, result.output
    assert "OpenClaw 列表" in result.output
    assert "账号: 2000003485" in result.output
    assert "region: cn-beijing-6" in result.output
    assert "总计: 145" in result.output


def test_openclaw_help_exposes_channel_and_gateway_commands():
    runner = CliRunner()

    result = runner.invoke(openclaw, ["--help"])

    assert result.exit_code == 0, result.output
    assert "channel" in result.output
    assert "gateway" in result.output
    assert "tui" in result.output


def test_openclaw_tui_help_states_no_local_openclaw_cli_required():
    runner = CliRunner()

    result = runner.invoke(openclaw, ["tui", "--help"])

    assert result.exit_code == 0, result.output
    assert "不需要本机安装 OpenClaw CLI" in result.output


def test_openclaw_tui_dry_run_does_not_resolve_or_connect(monkeypatch):
    runner = CliRunner()

    async def _forbidden_resolve(*_args, **_kwargs):
        raise AssertionError("agent detail should not be resolved")

    async def _forbidden_terminal(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.setattr(cmd_openclaw, "_resolve_openclaw_detail_or_raise", _forbidden_resolve)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _forbidden_terminal, raising=False)

    result = runner.invoke(
        openclaw,
        [
            "tui",
            "ar-demo-1",
            "--gateway-token",
            "gw-token",
            "--message",
            "你好",
            "--thinking",
            "medium",
            "--history-limit",
            "50",
            "--timeout-ms",
            "30000",
            "--deliver",
            "--dry-run",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "openclaw"
    assert payload["action"] == "tui"
    assert payload["request"]["agent_ref"] == "ar-demo-1"
    assert payload["request"]["mode"] == "tui"
    assert payload["request"]["gateway_token_provided"] is True
    assert payload["request"]["options"] == {
        "message": "你好",
        "thinking": "medium",
        "history_limit": 50,
        "timeout_ms": 30000,
        "deliver": True,
    }
    assert "gw-token" not in result.output


def test_openclaw_tui_uses_gateway_token_for_native_terminal(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_resolve(agent_ref, *, region):
        assert agent_ref == "ar-demo-1"
        assert region == "pre-online"
        return "pre-online", {
            "agent_id": "ar-demo-1",
            "name": "demo-openclaw",
            "status": "RUNNING",
            "framework": "openclaw",
            "endpoint": "https://openclaw.example.com",
            "api_key": "ak-agentengine",
            "openclaw_auth_mode": "token",
        }

    async def _fake_terminal(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cmd_openclaw, "_resolve_openclaw_detail_or_raise", _fake_resolve)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _fake_terminal, raising=False)

    result = runner.invoke(
        openclaw,
        [
            "tui",
            "ar-demo-1",
            "--region",
            "pre-online",
            "--gateway-token",
            "gw-token",
            "--session",
            "sess-1",
            "--message",
            "你好",
            "--thinking",
            "medium",
            "--history-limit",
            "50",
            "--timeout-ms",
            "30000",
            "--deliver",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["endpoint"] == "https://openclaw.example.com"
    assert captured["api_key"] == "gw-token"
    assert captured["session_id"] == "sess-1"
    assert captured["mode"] == "tui"
    assert captured["argv"] == []
    assert captured["options"] == {
        "message": "你好",
        "thinking": "medium",
        "history_limit": 50,
        "timeout_ms": 30000,
        "deliver": True,
    }


def test_openclaw_tui_uses_state_gateway_token_for_native_terminal(monkeypatch, tmp_path: Path):
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "agent_id": "ar-demo-1",
                "type": "openclaw",
                "framework": "openclaw",
                "endpoint": "https://openclaw.example.com",
                "api_key": "ak-agentengine",
                "openclaw_auth_mode": "token",
                "openclaw_gateway_token": "gw-token-from-state",
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_terminal(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _fake_terminal, raising=False)

    result = runner.invoke(openclaw, ["tui"])

    assert result.exit_code == 0, result.output
    assert captured["endpoint"] == "https://openclaw.example.com"
    assert captured["api_key"] == "gw-token-from-state"
    assert "gw-token-from-state" not in result.output


def test_openclaw_tui_requires_gateway_token_for_token_auth(monkeypatch):
    runner = CliRunner()

    async def _fake_resolve(_agent_ref, *, region):
        return region or "pre-online", {
            "agent_id": "ar-demo-1",
            "status": "RUNNING",
            "framework": "openclaw",
            "endpoint": "https://openclaw.example.com",
            "api_key": "ak-agentengine",
            "openclaw_auth_mode": "token",
        }

    async def _forbidden_terminal(**_kwargs):
        raise AssertionError("remote terminal should not be called")

    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)
    monkeypatch.setattr(cmd_openclaw, "_resolve_openclaw_detail_or_raise", _fake_resolve)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _forbidden_terminal, raising=False)

    result = runner.invoke(openclaw, ["tui", "ar-demo-1"])

    assert result.exit_code != 0
    assert "OPENCLAW_GATEWAY_TOKEN" in result.output


def test_openclaw_channel_connect_help_separates_channel_specific_options():
    runner = CliRunner()

    result = runner.invoke(openclaw, ["channel", "connect", "--help"])

    assert result.exit_code == 0, result.output
    assert "微信：扫码登录" in result.output
    assert "飞书：启动官方 onboarding 流程" in result.output
    assert "WPS 协作：写入开放平台 appId/appSecret" in result.output
    assert "仅 WPS 协作：开放平台应用 ID" in result.output
    assert "仅微信：在本地浏览器额外打开二维码链接" in result.output
    assert "--dm-policy=open 表示允许所有用户私聊" in result.output


def test_openclaw_gateway_ws_url_prints_dashboard_and_ws(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["gateway", "ws-url", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert "dashboard.example.com/s/lnk-demo" in result.output
    assert "wss://" in result.output
    assert "cookie-session" in result.output


def test_openclaw_gateway_logs_reads_agent_logs(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)

    result = runner.invoke(
        openclaw,
        ["gateway", "logs", "ar-demo-1", "--instance", "oc-0", "--log-type", "stdout"],
    )

    assert result.exit_code == 0, result.output
    assert "line-1" in result.output
    assert _FakeOpenClawDetailClient.last_log_kwargs["instance"] == "oc-0"
    assert _FakeOpenClawDetailClient.last_log_kwargs["log_type"] == "stdout"


def test_openclaw_gateway_doctor_checks_short_link_and_ws(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["gateway", "doctor", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert "dashboard_short_link" in result.output
    assert "cookie_ws_handshake" in result.output
    assert "gateway_rpc" in result.output


def test_openclaw_gateway_ws_url_allows_creating_when_gateway_is_reachable(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreatingDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["gateway", "ws-url", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert "dashboard.example.com/s/lnk-demo" in result.output
    assert "wss://" in result.output


def test_openclaw_gateway_doctor_continues_probe_when_status_is_creating(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreatingDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["gateway", "doctor", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert '"status": "CREATING"' in result.output
    assert '"dashboard_short_link"' in result.output
    assert '"ok": true' in result.output.lower()


def test_openclaw_gateway_doctor_fix_uses_control_plane_repair_for_failed_runtime(monkeypatch):
    runner = CliRunner()
    _FakeOpenClawFailedDetailClient.last_repair_kwargs = {}
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawFailedDetailClient)

    result = runner.invoke(openclaw, ["gateway", "doctor", "ar-demo-1", "--fix"])

    assert result.exit_code == 0, result.output
    assert '"repair_action": "doctor-fix"' in result.output
    assert _FakeOpenClawFailedDetailClient.last_repair_kwargs == {
        "agent_id": "ar-demo-1",
        "repair_action": "doctor-fix",
    }


def test_openclaw_repair_command_runs_doctor_fix_via_control_plane(monkeypatch):
    runner = CliRunner()
    _FakeOpenClawFailedDetailClient.last_repair_kwargs = {}
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawFailedDetailClient)

    result = runner.invoke(openclaw, ["repair", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert '"repair_action": "doctor-fix"' in result.output
    assert _FakeOpenClawFailedDetailClient.last_repair_kwargs == {
        "agent_id": "ar-demo-1",
        "repair_action": "doctor-fix",
    }


def test_openclaw_channel_status_uses_gateway_snapshot(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["channel", "status", "ar-demo-1", "--channel", "weixin", "--probe"])

    assert result.exit_code == 0, result.output
    assert '"connected": true' in result.output.lower()
    assert '"probe": true' in result.output.lower()


def test_openclaw_channel_status_allows_creating_when_gateway_is_reachable(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreatingDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)

    result = runner.invoke(openclaw, ["channel", "status", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert '"connected": true' in result.output.lower()


def test_openclaw_channel_enable_updates_weixin_account_config(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []
    async def _fake_sleep(*_args, **_kwargs):
        return None
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(
        openclaw,
        ["channel", "enable", "ar-demo-1", "--channel", "weixin", "--account-id", "wx-demo"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeGatewayClient.applied_configs
    config = _FakeGatewayClient.applied_configs[-1]["config"]
    assert config["channels"]["openclaw-weixin"]["accounts"]["wx-demo"]["enabled"] is True


def test_openclaw_channel_connect_weixin_prints_qr_url(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []
    _FakeGatewayClient.last_wait_kwargs = {}
    _FakeGatewayClient.disconnect_waits = []
    async def _fake_sleep(*_args, **_kwargs):
        return None
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert "https://qr.example.com/weixin-login" in result.output
    assert _FakeGatewayClient.applied_configs
    config = _FakeGatewayClient.applied_configs[-1]["config"]
    assert config["plugins"]["entries"]["openclaw-weixin"]["enabled"] is True
    assert config["channels"]["openclaw-weixin"]["accounts"]["default"]["enabled"] is True
    assert _FakeGatewayClient.last_wait_kwargs["account_id"] == "sess-1"


def test_openclaw_channel_connect_weixin_waits_for_gateway_restart(monkeypatch):
    runner = CliRunner()
    _FakeRestartingWeixinGatewayClient.applied_configs = []
    _FakeRestartingWeixinGatewayClient.last_wait_kwargs = {}
    _FakeRestartingWeixinGatewayClient.disconnect_waits = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeRestartingWeixinGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert _FakeRestartingWeixinGatewayClient.disconnect_waits == [5_000]
    assert _FakeRestartingWeixinGatewayClient.last_wait_kwargs["account_id"] == "sess-1"


def test_openclaw_channel_connect_weixin_maps_session_key_to_account_id(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.last_wait_kwargs = {}
    _FakeGatewayClient.disconnect_waits = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert _FakeGatewayClient.last_wait_kwargs == {
        "account_id": "sess-1",
        "session_key": None,
        "timeout_ms": 120_000,
    }


def test_openclaw_channel_connect_weixin_falls_back_to_remote_cli_without_web_login_rpc(monkeypatch):
    runner = CliRunner()
    _FakeWeixinGatewayWithoutWebLoginClient.applied_configs = []
    _FakeWeixinGatewayWithoutWebLoginClient.disconnect_waits = []
    captured: Dict[str, Any] = {}

    async def _fake_sleep(*_args, **_kwargs):
        return None

    async def _fake_terminal(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeWeixinGatewayWithoutWebLoginClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _fake_terminal, raising=False)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert captured["endpoint"] == "https://openclaw.example.com"
    assert captured["api_key"] is None
    assert captured["mode"] == "exec"
    assert captured["argv"] == ["openclaw", "channels", "login", "--channel", "openclaw-weixin"]
    assert _FakeWeixinGatewayWithoutWebLoginClient.applied_configs
    config = _FakeWeixinGatewayWithoutWebLoginClient.applied_configs[-1]["config"]
    assert config["plugins"]["entries"]["openclaw-weixin"]["enabled"] is True
    assert config["channels"]["openclaw-weixin"]["accounts"]["default"]["enabled"] is True
    assert '"mode": "remote_cli"' in result.output


def test_openclaw_channel_connect_weixin_falls_back_to_remote_cli_when_provider_unavailable(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_sleep(*_args, **_kwargs):
        return None

    async def _fake_terminal(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeWeixinGatewayProviderUnavailableClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(cmd_openclaw, "run_terminal_session", _fake_terminal, raising=False)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "weixin"])

    assert result.exit_code == 0, result.output
    assert captured["mode"] == "exec"
    assert captured["argv"] == ["openclaw", "channels", "login", "--channel", "openclaw-weixin"]
    assert "web login provider is not available" in result.output


def test_openclaw_channel_connect_feishu_applies_remote_config(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    async def _fake_onboarding(existing_app_id):
        assert existing_app_id is None
        return {
            "appId": "cli-app-id",
            "appSecret": "cli-app-secret",
            "domain": "lark",
            "userInfo": {"openId": "ou_demo"},
        }

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._run_feishu_onboarding", _fake_onboarding)

    result = runner.invoke(openclaw, ["channel", "connect", "ar-demo-1", "--channel", "feishu"])

    assert result.exit_code == 0, result.output
    assert _FakeGatewayClient.applied_configs
    config = _FakeGatewayClient.applied_configs[-1]["config"]
    assert config["plugins"]["entries"]["openclaw-lark"]["enabled"] is True
    assert config["channels"]["feishu"]["enabled"] is True
    assert config["channels"]["feishu"]["appId"] == "cli-app-id"
    assert config["channels"]["feishu"]["appSecret"] == "cli-app-secret"
    assert config["channels"]["feishu"]["domain"] == "lark"
    assert config["channels"]["feishu"]["allowFrom"] == ["ou_demo"]
    assert config["channels"]["feishu"]["groupAllowFrom"] == ["ou_demo"]


def test_should_auto_open_browser_on_local_macos(monkeypatch):
    from ksadk.cli.cmd_openclaw import _should_auto_open_browser

    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.sys.platform", "darwin")

    assert _should_auto_open_browser() is True


def test_should_not_auto_open_browser_over_ssh(monkeypatch):
    from ksadk.cli.cmd_openclaw import _should_auto_open_browser

    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.sys.platform", "darwin")

    assert _should_auto_open_browser() is False


def test_openclaw_channel_connect_wps_xiezuo_applies_flat_remote_config(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(
        openclaw,
        [
            "channel",
            "connect",
            "ar-demo-1",
            "--channel",
            "wps-xiezuo",
            "--app-id",
            "app-demo",
            "--app-secret",
            "secret-demo",
            "--dm-policy",
            "open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeGatewayClient.applied_configs
    config = _FakeGatewayClient.applied_configs[-1]["config"]
    assert config["plugins"]["entries"]["wps-xiezuo"]["enabled"] is True
    assert "wps-xiezuo" in config["plugins"]["allow"]
    channel = config["channels"]["wps-xiezuo"]
    assert channel["enabled"] is True
    assert channel["appId"] == "app-demo"
    assert channel["appSecret"] == "secret-demo"
    assert channel["baseUrl"] == "https://openapi.wps.cn"
    assert channel["dmPolicy"] == "open"
    assert channel["allowFrom"] == ["*"]
    assert channel["groupPolicy"] == "open"
    assert channel["sdk"] == {"enabled": True, "logLevel": "info"}
    assert channel["instantAck"]["text"] == "内容处理中，请稍候..."
    assert channel["mcp"]["enabled"] is True
    assert channel["mcp"]["mode"] == "app"
    assert "toolAllowlist" not in channel["mcp"]
    assert "accounts" not in channel
    assert "defaultAccountId" not in channel
    assert config["bindings"] == [
        {"type": "route", "agentId": "main", "match": {"channel": "wps-xiezuo"}}
    ]


def test_openclaw_channel_connect_wps_xiezuo_tolerates_reload_disconnect(monkeypatch):
    runner = CliRunner()
    _FakeConfigApplyReloadGatewayClient.applied_configs = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeConfigApplyReloadGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(
        openclaw,
        [
            "channel",
            "connect",
            "ar-demo-1",
            "--channel",
            "wps-xiezuo",
            "--app-id",
            "app-demo",
            "--app-secret",
            "secret-demo",
            "--dm-policy",
            "open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "gateway reload 期间连接短暂中断" in result.output
    assert _FakeConfigApplyReloadGatewayClient.applied_configs
    config = _FakeConfigApplyReloadGatewayClient.applied_configs[-1]["config"]
    assert config["channels"]["wps-xiezuo"]["appId"] == "app-demo"


def test_openclaw_channel_connect_wps_xiezuo_rejects_non_default_account(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(
        openclaw,
        [
            "channel",
            "connect",
            "ar-demo-1",
            "--channel",
            "wps-xiezuo",
            "--app-id",
            "app-demo",
            "--app-secret",
            "secret-demo",
            "--account-id",
            "tenant-a",
        ],
    )

    assert result.exit_code != 0
    assert "仅支持 default" in result.output


def test_openclaw_channel_connect_wps_xiezuo_requires_app_secret_when_dm_disabled(monkeypatch):
    runner = CliRunner()

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(
        openclaw,
        [
            "channel",
            "connect",
            "ar-demo-1",
            "--channel",
            "wps-xiezuo",
            "--app-id",
            "app-demo",
            "--dm-policy",
            "disabled",
        ],
    )

    assert result.exit_code != 0
    assert "必须提供 --app-secret" in result.output


def test_openclaw_channel_disable_wps_xiezuo_updates_flat_channel(monkeypatch):
    runner = CliRunner()
    _FakeGatewayClient.applied_configs = []

    async def _fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.asyncio.sleep", _fake_sleep)

    result = runner.invoke(openclaw, ["channel", "disable", "ar-demo-1", "--channel", "wps-xiezuo"])

    assert result.exit_code == 0, result.output
    assert _FakeGatewayClient.applied_configs
    config = _FakeGatewayClient.applied_configs[-1]["config"]
    assert config["channels"]["wps-xiezuo"]["enabled"] is False
    assert "accounts" not in config["channels"]["wps-xiezuo"]


def test_openclaw_channel_doctor_checks_snapshot_and_local_node(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeDoctorGatewayClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in {"node", "npx"} else None,
    )

    result = runner.invoke(openclaw, ["channel", "doctor", "ar-demo-1", "--channel", "feishu"])

    assert result.exit_code == 0, result.output
    assert "feishu_plugin_visible" in result.output
    assert "feishu_status_snapshot" in result.output
    assert "feishu_local_node" in result.output


def test_openclaw_channel_doctor_checks_wps_xiezuo_plugin_and_deps(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeDoctorGatewayClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw._check_wps_xiezuo_local_deps",
        lambda: {
            "ok": True,
            "node": "/usr/bin/node",
            "npm": "/usr/bin/npm",
        },
    )

    result = runner.invoke(openclaw, ["channel", "doctor", "ar-demo-1", "--channel", "wps-xiezuo"])

    assert result.exit_code == 0, result.output
    assert "wps_xiezuo_plugin_visible" in result.output
    assert "wps_xiezuo_status_snapshot" in result.output
    assert "wps_xiezuo_local_deps" in result.output


def test_openclaw_channel_doctor_treats_unconfigured_channels_as_connect_required(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeDoctorFreshGatewayClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in {"node", "npx"} else None,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw._check_wps_xiezuo_local_deps",
        lambda: {"ok": True, "node": "/usr/bin/node", "npm": "/usr/bin/npm"},
    )

    result = runner.invoke(openclaw, ["channel", "doctor", "ar-demo-1", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    checks = {item["name"]: item for item in payload["checks"]}
    assert payload["ok"] is False
    assert checks["weixin_qr_rpc"]["ok"] is False
    assert checks["weixin_qr_rpc"]["state"] == "connect_required"
    assert checks["feishu_status_snapshot"]["state"] == "connect_required"
    assert checks["wps_xiezuo_status_snapshot"]["state"] == "connect_required"


def test_openclaw_channel_doctor_keeps_configured_weixin_qr_rpc_as_hard_failure(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw.OpenClawGatewayClient", _FakeDoctorBrokenWeixinGatewayClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in {"node", "npx"} else None,
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw._check_wps_xiezuo_local_deps",
        lambda: {"ok": True, "node": "/usr/bin/node", "npm": "/usr/bin/npm"},
    )

    result = runner.invoke(openclaw, ["channel", "doctor", "ar-demo-1", "--channel", "weixin", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    checks = {item["name"]: item for item in payload["checks"]}
    assert payload["ok"] is False
    assert checks["weixin_qr_rpc"]["state"] == "missing"


def test_openclaw_deploy_supports_security_profile_flags(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_deploy_openclaw(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_openclaw._deploy_openclaw", _fake_deploy_openclaw)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(openclaw, ["deploy", "--strictest"])

    assert result.exit_code == 0, result.output
    assert captured["security_profile"] == "strictest"


def test_openclaw_deploy_forwards_custom_env_pairs(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_deploy_openclaw(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_openclaw._deploy_openclaw", _fake_deploy_openclaw)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--env",
            "FOO=bar",
            "--env",
            "OPENCLAW_GATEWAY_PORT=9090",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["extra_env"] == ("FOO=bar", "OPENCLAW_GATEWAY_PORT=9090")


def test_openclaw_deploy_forwards_explicit_memory_config(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_deploy_openclaw(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_openclaw._deploy_openclaw", _fake_deploy_openclaw)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--memory-system",
            "mem0",
            "--mem0-instance-id",
            "c17b20b1-faf7-4c98-91a7-38d1ee581ba1",
            "--mem0-instance-name",
            "mem-demo",
            "--mem0-region",
            "pre-online",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["memory_system"] == "mem0"
    assert captured["mem0_instance_id"] == "c17b20b1-faf7-4c98-91a7-38d1ee581ba1"
    assert captured["mem0_instance_name"] == "mem-demo"
    assert captured["mem0_region"] == "pre-online"


def test_openclaw_deploy_forwards_network_cli_options(monkeypatch):
    runner = CliRunner()
    captured: Dict[str, Any] = {}

    async def _fake_deploy_openclaw(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.cli.cmd_openclaw._deploy_openclaw", _fake_deploy_openclaw)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--disable-public-access",
            "--enable-vpc-access",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
            "--availability-zone",
            "cn-beijing-6b",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["enable_public_access"] is False
    assert captured["enable_vpc_access"] is True
    assert captured["vpc_id"] == "vpc-cli"
    assert captured["subnet_id"] == "subnet-cli"
    assert captured["security_group_id"] == "sg-cli"
    assert captured["availability_zone"] == "cn-beijing-6b"


def test_openclaw_deploy_rejects_mem0_without_instance_id():
    runner = CliRunner()

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--memory-system",
            "mem0",
        ],
    )

    assert result.exit_code != 0
    assert "--mem0-instance-id" in result.output


def test_openclaw_default_image_ref_tracks_current_runtime_tag():
    from ksadk.cli.cmd_openclaw import _resolve_image_ref

    assert _resolve_image_ref(None) == "ghcr.io/kingsoftcloud/agentengine-public/openclaw:2026.6.1"


def test_openclaw_deploy_create_payload_includes_network(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
            "--disable-public-access",
            "--enable-vpc-access",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
            "--availability-zone",
            "cn-beijing-6b",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.create_payload["network"] == {
        "enable_public_access": False,
        "enable_vpc_access": True,
        "vpc_id": "vpc-cli",
        "subnet_id": "subnet-cli",
        "security_group_id": "sg-cli",
        "availability_zone": "cn-beijing-6b",
    }


def test_openclaw_deploy_uses_init_project_name_when_name_is_omitted(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agentengine.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "custom-openclaw",
                "framework": "openclaw",
                "entry_point": "custom_openclaw/agent.py",
            }
        ),
        encoding="utf-8",
    )
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.create_payload["name"] == "custom-openclaw"


def test_openclaw_deploy_update_payload_includes_network(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "openclaw",
                "agent_id": "ar-existing-1",
                "name": "demo-openclaw",
                "endpoint": "https://existing.example.com",
                "api_key": "ak-existing",
            }
        ),
        encoding="utf-8",
    )
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
            "--enable-vpc-access",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.create_payload is None
    assert _FakeOpenClawCreateClient.update_payload["network"] == {
        "enable_vpc_access": True,
        "vpc_id": "vpc-cli",
        "subnet_id": "subnet-cli",
        "security_group_id": "sg-cli",
    }


def test_openclaw_deploy_update_payload_preserves_existing_config_by_default(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "openclaw",
                "agent_id": "ar-existing-1",
                "name": "demo-openclaw",
                "endpoint": "https://existing.example.com",
                "api_key": "ak-existing",
            }
        ),
        encoding="utf-8",
    )
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:new",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.create_payload is None
    assert _FakeOpenClawCreateClient.update_payload["artifact_path"] == (
        "ghcr.io/kingsoftcloud/agentengine-public/openclaw:new"
    )
    assert "env_vars" not in _FakeOpenClawCreateClient.update_payload
    assert "storage" not in _FakeOpenClawCreateClient.update_payload
    assert "network" not in _FakeOpenClawCreateClient.update_payload
    assert "memory_config" not in _FakeOpenClawCreateClient.update_payload


def test_openclaw_deploy_update_payload_includes_explicit_config(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".agentengine.state").write_text(
        yaml.safe_dump(
            {
                "type": "openclaw",
                "agent_id": "ar-existing-1",
                "name": "demo-openclaw",
                "endpoint": "https://existing.example.com",
                "api_key": "ak-existing",
            }
        ),
        encoding="utf-8",
    )
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:new",
            "--model-base-url",
            "https://model.example.com/v1",
            "--default-model",
            "glm-test",
            "--env",
            "APP_MODE=prod",
            "--storage-size-gi",
            "50",
            "--memory-system",
            "openclaw_default",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _FakeOpenClawCreateClient.update_payload
    assert any(item["Key"] == "APP_MODE" and item["Value"] == "prod" for item in payload["env_vars"])
    assert payload["storage"]["size_gi"] == 50
    assert payload["memory_config"] == {"memory_system": "openclaw_default"}


def test_openclaw_deploy_network_ids_imply_vpc_access(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.update_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.create_payload["network"]["enable_vpc_access"] is True


def test_openclaw_deploy_rejects_incomplete_vpc_network():
    runner = CliRunner()

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--enable-vpc-access",
            "--vpc-id",
            "vpc-cli",
        ],
    )

    assert result.exit_code != 0
    assert "VpcId、SubnetId、SecurityGroupId" in result.output


def test_openclaw_deploy_does_not_query_get_agent_when_quick_access_is_already_complete(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawCreateClient.get_agent_calls == 0


def test_openclaw_deploy_persists_gateway_token_from_extra_env(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawCreateClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
            "--env",
            "OPENCLAW_GATEWAY_AUTH_MODE=token",
            "--env",
            "OPENCLAW_GATEWAY_TOKEN=gw-token-from-deploy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "gw-token-from-deploy" not in result.output
    state = yaml.safe_load((tmp_path / ".agentengine.state").read_text())
    assert state["openclaw_auth_mode"] == "token"
    assert state["openclaw_gateway_token"] == "gw-token-from-deploy"


def test_openclaw_deploy_writes_only_configured_model_from_provider_catalog(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "deepseek-v4-pro")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    async def _fake_fetch_provider_model_catalog(**_kwargs):
        return [
            {
                "id": "glm-5.1",
                "context_window_tokens": 128_000,
                "max_output_tokens": 8_192,
            },
            {
                "id": "deepseek-v4-pro",
                "context_window_tokens": 1_000_000,
                "max_output_tokens": 384_000,
            },
            {
                "id": "kimi-k2.6",
                "context_window_tokens": 256_000,
                "max_output_tokens": 32_000,
            },
        ]

    monkeypatch.setattr(cmd_openclaw, "fetch_provider_model_catalog", _fake_fetch_provider_model_catalog)

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    env_vars = {
        item["Key"]: item["Value"]
        for item in _FakeOpenClawCreateClient.create_payload["env_vars"]
    }
    catalog = json.loads(env_vars["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == ["deepseek-v4-pro"]
    assert catalog[0]["contextWindow"] == 1_000_000
    assert catalog[0]["maxTokens"] == 384_000


def test_openclaw_deploy_writes_allowlisted_models_from_provider_catalog(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawCreateClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "deepseek-v4-pro")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://kspmas.ksyun.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENCLAW_MODEL_ALLOWLIST", "deepseek-v4-pro,glm-5.1")
    _FakeOpenClawCreateClient.create_payload = None
    _FakeOpenClawCreateClient.get_agent_calls = 0

    async def _fake_fetch_provider_model_catalog(**_kwargs):
        return [
            {
                "id": "glm-5.1",
                "context_window_tokens": 128_000,
                "max_output_tokens": 8_192,
            },
            {
                "id": "deepseek-v4-pro",
                "context_window_tokens": 1_000_000,
                "max_output_tokens": 384_000,
            },
            {
                "id": "kimi-k2.6",
                "context_window_tokens": 256_000,
                "max_output_tokens": 32_000,
            },
        ]

    monkeypatch.setattr(cmd_openclaw, "fetch_provider_model_catalog", _fake_fetch_provider_model_catalog)

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    env_vars = {
        item["Key"]: item["Value"]
        for item in _FakeOpenClawCreateClient.create_payload["env_vars"]
    }
    catalog = json.loads(env_vars["OPENCLAW_MODEL_CATALOG_JSON"])
    assert [item["id"] for item in catalog] == ["deepseek-v4-pro", "glm-5.1"]
    assert "kimi-k2.6" not in {item["id"] for item in catalog}


def test_openclaw_deploy_refreshes_quick_access_when_agent_id_is_immediate(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawImmediateAgentIdClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawImmediateAgentIdClient.get_agent_calls = 0

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawImmediateAgentIdClient.get_agent_calls == 1
    state = yaml.safe_load((tmp_path / ".agentengine.state").read_text())
    assert state["agent_id"] == "ar-created-2"
    assert state["endpoint"] == "https://fresh-openclaw.example.com"
    assert state["api_key"] == "ak-fresh-openclaw"


def test_openclaw_deploy_retries_transient_get_agent_not_found_until_api_key_is_ready(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDelayedAccessClient)
    monkeypatch.setattr("ksadk.cli.cmd_openclaw._GLOBAL_ENV_CACHE", {})
    monkeypatch.chdir(tmp_path)
    _FakeOpenClawDelayedAccessClient.get_agent_calls = 0
    _FakeOpenClawDelayedAccessClient.suppression_used = False

    result = runner.invoke(
        openclaw,
        [
            "deploy",
            "--name",
            "demo-openclaw",
            "--image",
            "ghcr.io/kingsoftcloud/agentengine-public/openclaw:test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeOpenClawDelayedAccessClient.suppression_used is True
    assert _FakeOpenClawDelayedAccessClient.get_agent_calls == 4
    state = yaml.safe_load((tmp_path / ".agentengine.state").read_text())
    assert state["agent_id"] == "ar-created-delayed"
    assert state["endpoint"] == "https://ready-openclaw.example.com"
    assert state["api_key"] == "ak-ready-openclaw"


def test_openclaw_flatten_agent_detail_reads_framework_and_region_from_deployment():
    detail = cmd_openclaw._flatten_agent_detail(
        {
            "basic": {
                "agent_id": "ar-openclaw-demo",
                "name": "demo-openclaw",
                "status": "running",
            },
            "quick_access": {
                "public_endpoint": "https://demo-openclaw.example.com",
                "api_key": "ak-demo-openclaw",
            },
            "deployment": {
                "framework": "openclaw",
                "region": "pre-online",
                "artifact_path": "hub/openclaw:test",
            },
        }
    )

    assert detail["framework"] == "openclaw"
    assert detail["region"] == "pre-online"
    assert detail["api_key"] == "ak-demo-openclaw"


def test_version_list_supports_dry_run(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)
    monkeypatch.setenv("KSYUN_REGION", "cn-beijing-6")

    result = runner.invoke(version, ["list", "--agent", "demo-agent", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_top_level_delete_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_delete,
        ["ar-123", "--account-id", "2000003485", "--force", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert provider.calls
    assert provider.calls[0][0] == "ar-123"


def test_top_level_destroy_accepts_yes_alias(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_cmd,
        ["ar-456", "--account-id", "2000003485", "--yes", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert provider.calls
    assert provider.calls[0][0] == "ar-456"


def test_openclaw_destroy_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(openclaw, ["destroy", "ar-demo-1", "--force", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_mcp_destroy_accepts_force_alias(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        mcp,
        ["destroy", "mcp-123", "--force", "--dry-run"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert "Dry Run Completed" in result.output
    assert _FakeDryRunClient.last_init_kwargs.get("dry_run") is True


def test_root_cli_registers_delete_alias():
    _register_commands()
    assert "agent" in cli.commands
    assert "delete" in cli.commands
    assert "destroy" in cli.commands
    assert cli.get_command(None, "delete").hidden is True
    assert cli.get_command(None, "destroy").hidden is True


def test_root_help_shows_canonical_commands_only():
    runner = CliRunner()
    _register_commands()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "agentengine agent" in result.output
    assert "agentengine status" not in result.output
    assert "agentengine invoke" not in result.output
    assert "agentengine delete" not in result.output
    assert "agentengine destroy" not in result.output


def test_agent_group_exposes_canonical_subcommands():
    runner = CliRunner()

    result = runner.invoke(agent, ["--help"])

    assert result.exit_code == 0, result.output
    assert "list" in result.output
    assert "status" in result.output
    assert "invoke" in result.output
    assert "delete" in result.output


def test_root_status_all_routes_with_compatibility_hint(monkeypatch):
    runner = CliRunner()
    _register_commands()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    result = runner.invoke(
        cli,
        ["status", "--all", "--account-id", "2000003485", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "agentengine agent list" in result.output
    assert "Dry Run Completed" in result.output


def test_root_invoke_alias_still_callable_with_hint(monkeypatch):
    runner = CliRunner()
    _register_commands()
    invoked = {}

    def fake_invoke_tui(
        endpoint,
        api_key,
        session_id,
        insecure,
        model,
        show_thinking,
        api_format=None,
        responses_session_header=None,
    ):
        return invoked.setdefault("endpoint", endpoint)

    monkeypatch.setattr(
        "ksadk.cli.cmd_invoke._invoke_tui",
        fake_invoke_tui,
    )

    result = runner.invoke(cli, ["invoke", "--endpoint", "http://demo.local"])

    assert result.exit_code == 0, result.output
    assert "agentengine agent invoke" in result.output
    assert invoked["endpoint"] == "http://demo.local"


def test_legacy_root_help_points_to_canonical_commands():
    runner = CliRunner()
    _register_commands()

    result = runner.invoke(cli, ["status", "--help"])

    assert result.exit_code == 0, result.output
    assert "这是兼容入口" in result.output
    assert "agentengine agent status --help" in result.output


def test_top_level_delete_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_delete,
        ["ar-123", "ar-456", "--account-id", "2000003485", "--force", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert [call[0] for call in provider.calls] == ["ar-123", "ar-456"]


def test_top_level_destroy_supports_repeated_agent_option(monkeypatch):
    runner = CliRunner()
    provider = _FakeDeleteProvider()
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        destroy_cmd,
        ["--agent", "ar-123", "--agent", "ar-456", "--account-id", "2000003485", "--yes", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert [call[0] for call in provider.calls] == ["ar-123", "ar-456"]


def test_openclaw_destroy_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_agents = []
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(openclaw, ["destroy", "ar-demo-1", "ar-demo-2", "--force"])

    assert result.exit_code == 0, result.output
    assert _FakeBatchDeleteClient.deleted_agents == ["ar-demo-1", "ar-demo-2"]


def test_openclaw_delete_passes_result_styles_to_descriptor(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_agents = []
    captured = {}

    def _fake_render_descriptor_status(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_openclaw.render_descriptor_status",
        _fake_render_descriptor_status,
    )

    result = runner.invoke(openclaw, ["delete", "ar-demo-1", "--yes"])

    assert result.exit_code == 0, result.output
    assert captured["fields"][1] == ("已删除", "ar-demo-1", "ok")
    assert captured["fields"][2] == ("失败", "-", "muted")
    assert captured["next_steps"] == (
        "agentengine openclaw list",
        "agentengine openclaw deploy",
    )


def test_openclaw_status_shows_langfuse_trace_url(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeOpenClawDetailClient)

    result = runner.invoke(openclaw, ["status", "ar-demo-1"])

    assert result.exit_code == 0, result.output
    assert "Langfuse" in result.output
    assert "https://trace.example.com/project/aropenclaw1/traces" in result.output


def test_mcp_destroy_supports_multiple_ids(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_mcps = []
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_mcp.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )

    result = runner.invoke(
        mcp,
        ["destroy", "mcp-123", "mcp-456", "--force"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert _FakeBatchDeleteClient.deleted_mcps == ["mcp-123", "mcp-456"]


def test_mcp_delete_passes_result_styles_to_descriptor(monkeypatch):
    runner = CliRunner()
    _FakeBatchDeleteClient.deleted_mcps = []
    captured = {}

    def _fake_render_descriptor_status(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeBatchDeleteClient)
    monkeypatch.setattr(
        "ksadk.cli.cmd_mcp.run_async_with_dry_run",
        lambda coro, dry_run: asyncio.run(coro),
    )
    monkeypatch.setattr(
        "ksadk.cli.cmd_mcp.render_descriptor_status",
        _fake_render_descriptor_status,
    )

    result = runner.invoke(
        mcp,
        ["delete", "mcp-123", "--yes"],
        env={"AGENTENGINE_SERVER_URL": "http://example.com"},
    )

    assert result.exit_code == 0, result.output
    assert captured["fields"][1] == ("已删除", "mcp-123", "ok")
    assert captured["fields"][2] == ("失败", "-", "muted")
    assert captured["next_steps"] == (
        "agentengine mcp list",
        "agentengine mcp deploy",
    )


def test_agent_delete_json_requires_yes(monkeypatch):
    runner = CliRunner()
    _register_commands()

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)

    result = runner.invoke(
        cli,
        ["--output", "json", "agent", "delete", "ar-123", "--account-id", "2000003485"],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert "--yes" in payload["error"]["message"]


def test_agent_delete_json_returns_error_on_partial_failure(monkeypatch):
    runner = CliRunner()
    _register_commands()
    provider = _FakePartialDeleteProvider({"ar-1": True, "ar-2": False})

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)
    monkeypatch.setattr("ksadk.cli.cmd_destroy.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cli,
        ["--output", "json", "agent", "delete", "ar-1", "ar-2", "--account-id", "2000003485", "--yes"],
    )

    assert result.exit_code == 6, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "remote_error"
    assert payload["error"]["details"]["deleted"] == ["ar-1"]
    assert payload["error"]["details"]["failed"] == ["ar-2"]


def test_agent_delete_cancel_returns_cancelled_exit_code(monkeypatch):
    runner = CliRunner()
    _register_commands()

    async def _resolve(ids, _region, _account_id):
        return ids

    monkeypatch.setattr("ksadk.cli.cmd_destroy._resolve_agent_ids", _resolve)

    result = runner.invoke(
        cli,
        ["agent", "delete", "ar-1", "--account-id", "2000003485"],
        input="n\n",
    )

    assert result.exit_code == 7, result.output
    assert "已取消" in result.output


def test_serverless_destroy_cleans_local_state_only_after_success(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = True
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": False}),
        )
    )

    assert success is True
    assert _FakeDeleteClient.deleted_agents == ["ar-demo"]
    assert state_file.exists() is False


def test_serverless_destroy_keeps_local_state_on_dry_run(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    class _DryRunDeleteClient(_FakeDeleteClient):
        async def delete_agent(self, agent_id):
            raise DryRunExit(
                "dry-run",
                payload={"method": "POST", "url": "https://example.com", "curl": "curl -X POST https://example.com"},
            )

    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _DryRunDeleteClient)

    try:
        asyncio.run(
            provider.destroy(
                "ar-demo",
                DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": True}),
            )
        )
    except DryRunExit:
        pass
    else:
        raise AssertionError("DryRunExit should bubble for CLI handling")

    assert state_file.exists() is True


def test_serverless_destroy_keeps_local_state_when_remote_delete_fails(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    state_file = tmp_path / ".agentengine.state"
    state_file.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = False
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(provider="serverless", region="cn-beijing-6", extra={"dry_run": False}),
        )
    )

    assert success is False
    assert state_file.exists() is True


def test_serverless_destroy_uses_explicit_project_dir_for_state_cleanup(tmp_path, monkeypatch):
    provider = ServerlessProvider()
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()

    project_state = project_dir / ".agentengine.state"
    project_state.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")
    other_state = other_dir / ".agentengine.state"
    other_state.write_text(yaml.safe_dump({"agent_id": "ar-demo"}), encoding="utf-8")

    _FakeDeleteClient.deleted_agents = []
    _FakeDeleteClient.should_succeed = True
    monkeypatch.chdir(other_dir)
    monkeypatch.setattr("ksadk.deployment.providers.serverless.AgentEngineClient", _FakeDeleteClient)

    success = asyncio.run(
        provider.destroy(
            "ar-demo",
            DeployTarget(
                provider="serverless",
                region="cn-beijing-6",
                extra={"dry_run": False, "project_dir": str(project_dir)},
            ),
        )
    )

    assert success is True
    assert project_state.exists() is False
    assert other_state.exists() is True
