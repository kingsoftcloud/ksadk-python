from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from ksadk.api.client import DryRunExit
from ksadk.cli import _register_commands, cli
from ksadk.cli import cmd_dashboard, cmd_deploy, cmd_launch, cmd_mcp
from ksadk.cli.cmd_build import build
from ksadk.cli.cmd_mcp import mcp
from ksadk.deployment.base import DeployResult, DeployStatus, PackageInfo
from ksadk.builders.base import BuildResult


def _parse_json(output: str) -> dict:
    return json.loads(output.strip())


def test_config_show_json_envelope(tmp_path: Path, monkeypatch):
    _register_commands()
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    (tmp_path / "agentengine.yaml").write_text(
        yaml.safe_dump({"name": "demo-agent", "framework": "langgraph", "region": "cn-beijing-6"}),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("OPENAI_MODEL_NAME=demo-model\n", encoding="utf-8")
    monkeypatch.setattr(
        "ksadk.configs.global_config.load_global_config",
        lambda: {"cloud": {"KSYUN_REGION": "cn-guangzhou-1"}},
    )
    monkeypatch.setattr(
        "ksadk.configs.global_config.get_env_from_global_config",
        lambda: {"KSYUN_REGION": "cn-guangzhou-1"},
    )
    monkeypatch.setattr(
        "ksadk.configs.global_config.get_global_config_path",
        lambda: tmp_path / ".agentengine" / "settings.json",
    )

    result = runner.invoke(cli, ["--output", "json", "config", "show"])

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "status"
    assert payload["resource"] == "config"
    assert payload["item"]["project_config"]["name"] == "demo-agent"
    assert payload["item"]["effective_env"]["OPENAI_MODEL_NAME"] == "demo-model"
    assert payload["item"]["effective_env"]["KSYUN_REGION"] == "cn-guangzhou-1"


def test_config_set_json_envelope_and_file_updates(tmp_path: Path, monkeypatch):
    _register_commands()
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "config",
            "set",
            "region=cn-beijing-6",
            "OPENAI_MODEL_NAME=glm-5.1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "config"
    assert payload["action"] == "set"
    assert sorted(payload["result"]["updated_project_keys"]) == ["region"]
    assert sorted(payload["result"]["updated_env_keys"]) == ["KSYUN_REGION", "OPENAI_MODEL_NAME"]

    project_config = yaml.safe_load((tmp_path / "agentengine.yaml").read_text(encoding="utf-8-sig"))
    env_text = (tmp_path / ".env").read_text(encoding="utf-8-sig")
    assert project_config["region"] == "cn-beijing-6"
    assert "OPENAI_MODEL_NAME=glm-5.1" in env_text
    assert "KSYUN_REGION=cn-beijing-6" in env_text


def test_config_set_uppercase_env_var_updates_project_env(tmp_path: Path, monkeypatch):
    _register_commands()
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli,
        [
            "--output",
            "json",
            "config",
            "set",
            "AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["result"]["updated_project_keys"] == []
    assert payload["result"]["updated_env_keys"] == ["AGENTENGINE_SERVER_URL"]

    env_text = (tmp_path / ".env").read_text(encoding="utf-8-sig")
    assert "AGENTENGINE_SERVER_URL=http://aicp.inner.api.ksyun.com" in env_text
    assert not (tmp_path / "agentengine.yaml").exists()


def test_dashboard_open_json_does_not_open_browser(monkeypatch):
    runner = CliRunner()
    opened_urls: list[str] = []

    async def _fake_resolve_agent_detail(*_args, **_kwargs):
        return (
            {
                "agent_id": "ar-demo",
                "name": "demo-agent",
                "framework": "langgraph",
                "endpoint": "https://agent.example.com",
            },
            type("Ref", (), {"source": "cli", "source_text": "CLI", "value": "ar-demo"})(),
            False,
        )

    async def _fake_create_dashboard_access_link(**_kwargs):
        return {
            "link_id": "lnk-demo",
            "expires_at": None,
            "access_url": "https://dashboard.example.com/share/lnk-demo",
        }

    monkeypatch.setattr(cmd_dashboard, "_resolve_agent_detail", _fake_resolve_agent_detail)
    monkeypatch.setattr(cmd_dashboard, "_create_dashboard_access_link", _fake_create_dashboard_access_link)
    monkeypatch.setattr(cmd_dashboard, "load_state", lambda _cwd: {})
    monkeypatch.setattr(cmd_dashboard.webbrowser, "open", lambda url: opened_urls.append(url))

    result = runner.invoke(cmd_dashboard.dashboard, ["open", "ar-demo", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "dashboard_share"
    assert payload["action"] == "open"
    assert payload["result"]["url"] == "https://dashboard.example.com/share/lnk-demo"
    assert opened_urls == []


def test_dashboard_share_revoke_json_requires_yes(monkeypatch):
    runner = CliRunner()

    async def _should_not_run(**_kwargs):
        raise AssertionError("delete should not be called without --yes")

    monkeypatch.setattr(cmd_dashboard, "_delete_dashboard_access_link", _should_not_run)

    result = runner.invoke(cmd_dashboard.dashboard, ["share", "revoke", "lnk-demo", "--output", "json"])

    assert result.exit_code == 2, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert "--yes" in payload["error"]["message"] or "--yes" in "".join(payload["hints"])


class _FakeMCPDetectionResult:
    is_valid = True
    entry_point = "mcp_server.py"
    mcp_variable = "mcp"
    tools = ["search", "fetch"]


class _FakeMCPDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def detect(self):
        return _FakeMCPDetectionResult()


class _FakeMCPBuildResult:
    success = True
    artifact_path = Path("/tmp/demo-mcp.zip")
    error_message = ""
    metadata = {}


async def _fake_build_code_artifact(*_args, **_kwargs):
    build_result = BuildResult(
        success=True,
        artifact_path=Path("/tmp/demo-mcp.zip"),
        artifact_size=1234,
        metadata={"framework": "mcp"},
    )
    return build_result, "ks3://demo-bucket/mcps/demo-mcp/code_20260322120000.zip"


async def _fake_build_mcp_async(*_args, **_kwargs):
    return {
        "framework": "mcp",
        "artifact_type": "code",
        "artifact_reference": "ks3://demo-bucket/mcps/demo-mcp/code_fake.zip",
        "artifact_built": True,
        "artifact_source": "built",
        "artifact_reused": False,
        "push": True,
    }


class _FakeMCPClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_mcp(self, _request):
        return {
            "mcp_id": "mcp-demo",
            "endpoint": "https://mcp.example.com",
            "api_key": "secret",
        }

    async def update_mcp(self, _mcp_id, _request):
        raise AssertionError("update path should not be used in this test")


class _FakeMCPDryRunClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_mcp(self, request):
        raise DryRunExit("dry-run", payload={"body": request})

    async def close(self):
        return None


def test_mcp_deploy_json_envelope(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr(cmd_mcp, "_build_code_artifact", _fake_build_code_artifact)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeMCPClient)

    result = runner.invoke(mcp, ["deploy", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "deploy"
    assert payload["result"]["artifact_type"] == "code"
    assert payload["result"]["artifact_reference"] == "ks3://demo-bucket/mcps/demo-mcp/code_20260322120000.zip"
    assert payload["result"]["mcp_id"] == "mcp-demo"
    assert payload["result"]["mcp_url"] == "https://mcp.example.com/mcp"


def test_mcp_deploy_dry_run_json_envelope(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeMCPDryRunClient)

    def _should_not_build(*_args, **_kwargs):
        raise AssertionError("Dry run should not build artifacts")

    monkeypatch.setattr(cmd_mcp, "_build_code_artifact", _should_not_build)

    result = runner.invoke(
        mcp,
        ["deploy", str(tmp_path), "--dry-run", "--output", "json", "--ks3-bucket", "demo-bucket"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "deploy"
    assert payload["request"]["body"]["artifact_type"] == "Code"
    assert payload["plan"]["artifact"]["reference"].startswith("ks3://demo-bucket/")


async def _fake_build_mcp_async(**_kwargs):
    return {
        "framework": "mcp",
        "artifact_type": "code",
        "artifact_source": "built",
        "artifact_reused": False,
        "artifact_built": True,
        "artifact_reference": "ks3://demo-bucket/mcps/demo-mcp/code_fake.zip",
        "push": True,
        "region": "cn-beijing-6",
        "mcp_name": "demo-mcp",
        "tools": ["ping", "add"],
    }


def test_mcp_build_json_envelope(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_mcp, "_build_mcp_async", _fake_build_mcp_async)

    result = runner.invoke(mcp, ["build", str(tmp_path), "--artifact-type", "Code", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "build"
    assert payload["result"]["artifact_type"] == "code"
    assert payload["result"]["artifact_reference"] == "ks3://demo-bucket/mcps/demo-mcp/code_fake.zip"


class _FakeBuildResult:
    def __init__(self):
        self.success = True
        self.error_message = ""
        self.metadata = {"framework": "langgraph", "agent_name": "demo-agent", "reused": False}
        self.artifact_path = Path("/tmp/demo-agent.zip")
        self.artifact_size_mb = 12.5


class _FakeCodeBuilder:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def build(self):
        return _FakeBuildResult()


class _FakeContainerBuildResult:
    def __init__(self):
        self.success = True
        self.error_message = ""
        self.metadata = {"framework": "langgraph", "image": "hub.kce.ksyun.com/demo/demo-agent:latest", "reused": False}
        self.artifact_path = Path("/tmp/demo-image.tar")
        self.artifact_size_mb = 25.0


class _FakeContainerBuilderPushFailure:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def build(self):
        return _FakeContainerBuildResult()

    def push(self, _image):
        return False


def test_build_json_envelope(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.setattr("ksadk.builders.CodeBuilder", _FakeCodeBuilder)

    result = runner.invoke(build, [str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "build"
    assert payload["result"]["artifact_type"] == "code"
    assert payload["result"]["artifact_built"] is True


def test_build_push_failure_returns_structured_json_error(monkeypatch, tmp_path: Path):
    _register_commands()
    runner = CliRunner()
    monkeypatch.setattr("ksadk.builders.ContainerBuilder", _FakeContainerBuilderPushFailure)

    result = runner.invoke(
        cli,
        ["--output", "json", "build", str(tmp_path), "--mode", "container", "--push"],
    )

    assert result.exit_code == 6, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "remote_error"
    assert payload["error"]["details"]["image"] == "hub.kce.ksyun.com/demo/demo-agent:latest"


class _FakeDetectionType:
    value = "langgraph"


class _FakeDetectionResult:
    type = _FakeDetectionType()
    name = "langgraph"
    entry_point = "agent.py"


class _FakeProvider:
    def __init__(self):
        self.calls = []

    async def validate_config(self, _target):
        self.calls.append("validate")
        return True, ""

    async def package(self, project_dir, _detection_result, _config):
        self.calls.append("package")
        return PackageInfo(
            name="demo-agent",
            framework="langgraph",
            build_dir=str(Path(project_dir) / ".agentengine" / "build"),
            project_dir=str(project_dir),
            metadata={},
        )

    async def build(self, package_info, _target):
        self.calls.append("build")
        package_info.metadata["ks3_path"] = "ks3://bucket/agents/demo-agent/code_20260320170000.zip"
        return package_info

    async def deploy(self, _package_info, _target):
        self.calls.append("deploy")
        return DeployResult(
            status=DeployStatus.DEPLOYING,
            agent_id="ar-demo",
            agent_name="demo-agent",
            endpoint="http://demo-endpoint",
            message="ok",
        )


class _FakeWorkflowDryRunProvider(_FakeProvider):
    async def deploy(self, package_info, target):
        self.calls.append("deploy")
        artifact_reference = (
            package_info.metadata.get("ks3_path")
            or package_info.image
            or target.extra.get("ks3_path")
            or target.extra.get("image")
            or ""
        )
        return DeployResult(
            status=DeployStatus.SKIPPED,
            message="dry run",
            metadata={
                "dry_run_request": {
                    "method": "POST",
                    "url": "https://agentengine.example.com/agentengine/api/v1/CreateAgent",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"ArtifactPath": artifact_reference, "Name": package_info.name},
                    "curl": "curl -X POST https://agentengine.example.com/agentengine/api/v1/CreateAgent",
                }
            },
        )


class _NoBuildDuringDryRunProvider(_FakeWorkflowDryRunProvider):
    async def build(self, package_info, _target):
        raise AssertionError("build should not run during dry-run")


def test_deploy_json_envelope(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_deploy.deploy,
        [str(tmp_path), "--account-id", "2000003485", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "deploy"
    assert payload["result"]["agent_id"] == "ar-demo"
    assert payload["result"]["endpoint"] == "http://demo-endpoint"


def test_launch_json_envelope(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [str(tmp_path), "--account-id", "2000003485", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "result"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "launch"
    assert payload["result"]["agent_id"] == "ar-demo"
    assert payload["result"]["endpoint"] == "http://demo-endpoint"


def test_deploy_dry_run_json_envelope_includes_local_plan_and_remote_curl(tmp_path: Path, monkeypatch):
    provider = _NoBuildDuringDryRunProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_deploy.deploy,
        [str(tmp_path), "--account-id", "2000003485", "--dry-run", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "dry_run"
    assert payload["resource"] == "workflow"
    assert payload["action"] == "deploy"
    assert payload["plan"]["artifact"]["should_build"] is True
    assert payload["plan"]["artifact"]["will_build"] is False
    assert payload["plan"]["artifact"]["should_local_build"] is True
    assert payload["plan"]["artifact"]["will_local_build"] is False
    assert payload["plan"]["artifact"]["should_publish"] is True
    assert payload["plan"]["artifact"]["will_publish"] is False
    assert payload["plan"]["artifact"]["source"] == "planned_build"
    assert payload["plan"]["artifact"]["reference_is_predicted"] is True
    assert [step["name"] for step in payload["plan"]["steps"]] == [
        "validate_config",
        "package",
        "local_build",
        "artifact_publish",
        "deploy_request",
    ]
    assert payload["plan"]["steps"][-1]["name"] == "deploy_request"
    assert payload["plan"]["steps"][2]["will_run"] is False
    assert payload["plan"]["steps"][2]["planned"] is True
    assert payload["plan"]["steps"][2]["reason"] == "dry_run_prediction"
    assert payload["plan"]["steps"][3]["kind"] == "remote"
    assert payload["plan"]["steps"][3]["will_run"] is False
    assert payload["plan"]["steps"][3]["planned"] is True
    assert payload["plan"]["steps"][3]["reason"] == "dry_run_prediction"
    assert "CreateAgent" in payload["request"]["curl"]
    assert payload["request"]["body"]["ArtifactPath"].startswith("ks3://agentengine-2000003485-cn-beijing-6/")


def test_deploy_dry_run_pretty_output_groups_summary_plan_and_request(tmp_path: Path, monkeypatch):
    provider = _NoBuildDuringDryRunProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_deploy.deploy,
        [str(tmp_path), "--account-id", "2000003485", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "执行摘要" in result.output
    assert "本次执行" in result.output
    assert "仅计划" in result.output
    assert "local_build" in result.output
    assert "artifact_publish" in result.output
    assert "远端请求" in result.output
    assert "请求方法" in result.output
    assert "请求地址" in result.output
    assert "请求字段" in result.output
    assert "Curl:" in result.output


def test_launch_dry_run_json_envelope_tracks_external_artifact_plan(tmp_path: Path, monkeypatch):
    provider = _FakeWorkflowDryRunProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [
            str(tmp_path),
            "--account-id",
            "2000003485",
            "--artifact-type",
            "Container",
            "--image",
            "hub.kce.ksyun.com/demo/demo-agent:latest",
            "--dry-run",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "dry_run"
    assert payload["action"] == "launch"
    assert payload["plan"]["artifact"]["should_build"] is False
    assert payload["plan"]["artifact"]["should_local_build"] is False
    assert payload["plan"]["artifact"]["should_publish"] is False
    assert payload["plan"]["artifact"]["explicit_ref_option"] == "--image"
    assert payload["plan"]["artifact"]["source"] == "external"
    assert payload["plan"]["steps"][2]["name"] == "local_build"
    assert payload["plan"]["steps"][2]["will_run"] is False
    assert payload["plan"]["steps"][2]["reason"] == "explicit_reference"
    assert payload["plan"]["steps"][3]["name"] == "artifact_publish"
    assert payload["plan"]["steps"][3]["will_run"] is False
    assert payload["plan"]["steps"][3]["reason"] == "explicit_reference"
    assert payload["request"]["body"]["ArtifactPath"] == "hub.kce.ksyun.com/demo/demo-agent:latest"


def test_launch_dry_run_json_skips_real_build_and_predicts_container_reference(tmp_path: Path, monkeypatch):
    provider = _NoBuildDuringDryRunProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [
            str(tmp_path),
            "--account-id",
            "2000003485",
            "--artifact-type",
            "Container",
            "--registry",
            "hub.kce.ksyun.com/demo",
            "--dry-run",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["kind"] == "dry_run"
    assert payload["plan"]["artifact"]["should_build"] is True
    assert payload["plan"]["artifact"]["will_build"] is False
    assert payload["plan"]["artifact"]["should_publish"] is True
    assert payload["plan"]["artifact"]["will_publish"] is False
    assert payload["plan"]["artifact"]["source"] == "planned_build"
    assert payload["plan"]["steps"][2]["name"] == "local_build"
    assert payload["plan"]["steps"][2]["reason"] == "dry_run_prediction"
    assert payload["plan"]["steps"][3]["name"] == "artifact_publish"
    assert payload["plan"]["steps"][3]["reason"] == "dry_run_prediction"
    assert payload["request"]["body"]["ArtifactPath"] == "hub.kce.ksyun.com/demo/demo-agent:dry-run"


def test_deploy_reuses_cached_artifact_without_rebuild(tmp_path: Path, monkeypatch):
    provider = _NoBuildDuringDryRunProvider()
    runner = CliRunner()
    metadata_dir = tmp_path / ".agentengine"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "build-metadata.json").write_text(
        json.dumps({"metadata": {"ks3_path": "ks3://bucket/agents/demo-agent/cached.zip"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_deploy.deploy,
        [str(tmp_path), "--account-id", "2000003485", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["result"]["artifact_source"] == "cached"
    assert payload["result"]["artifact_reused"] is True
    assert payload["result"]["artifact_built"] is False
    assert payload["result"]["artifact_reference"] == "ks3://bucket/agents/demo-agent/cached.zip"


def test_launch_reuses_cached_container_artifact_without_rebuild(tmp_path: Path, monkeypatch):
    provider = _NoBuildDuringDryRunProvider()
    runner = CliRunner()
    metadata_dir = tmp_path / ".agentengine"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "build-metadata.json").write_text(
        json.dumps({"image": "hub.kce.ksyun.com/demo/demo-agent:cached", "metadata": {"image": "hub.kce.ksyun.com/demo/demo-agent:cached"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [str(tmp_path), "--account-id", "2000003485", "--artifact-type", "Container", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json(result.output)
    assert payload["result"]["artifact_source"] == "cached"
    assert payload["result"]["artifact_reused"] is True
    assert payload["result"]["artifact_built"] is False
    assert payload["result"]["artifact_reference"] == "hub.kce.ksyun.com/demo/demo-agent:cached"
