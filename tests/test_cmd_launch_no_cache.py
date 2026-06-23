import asyncio
from pathlib import Path

from click.testing import CliRunner

from ksadk.cli import cmd_launch
from ksadk.deployment.base import DeployResult, DeployStatus, PackageInfo


class _FakeDetectionType:
    value = "langgraph"


class _FakeDetectionResult:
    type = _FakeDetectionType()
    name = "langgraph"
    entry_point = "agent.py"


class _FakeProvider:
    def __init__(self):
        self.calls = []
        self.package_metadata_file_exists = None
        self.last_target = None

    async def validate_config(self, _target):
        self.last_target = _target
        self.calls.append("validate")
        return True, ""

    async def package(self, project_dir, _detection_result, _config):
        self.calls.append("package")
        metadata_file = Path(project_dir) / ".agentengine" / "build-metadata.json"
        self.package_metadata_file_exists = metadata_file.exists()
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


def test_launch_no_cache_triggers_build_and_clears_metadata(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    metadata_dir = tmp_path / ".agentengine"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "build-metadata.json").write_text('{"metadata":{"ks3_path":"ks3://old/path.zip"}}', encoding="utf-8")

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    asyncio.run(
        cmd_launch._launch_async(
            agent_dir=str(tmp_path),
            target="serverless",
            name=None,
            region="cn-beijing-6",
            account_id="2000003485",
            observability=True,
            no_cache=True,
            port=8000,
            namespace="default",
            registry=None,
            ks3_bucket=None,
            ks3_path=None,
            image=None,
            ui_profile=None,
            ui_path=None,
            ui_url=None,
            dry_run=False,
            artifact_type="Code",
            no_version=True,
            auto_rollback=False,
        )
    )

    assert provider.package_metadata_file_exists is False
    assert provider.calls == ["validate", "package", "build", "deploy"]


def test_launch_no_cache_warns_when_explicit_ks3_path_is_supplied(tmp_path: Path, monkeypatch, capsys):
    provider = _FakeProvider()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    asyncio.run(
        cmd_launch._launch_async(
            agent_dir=str(tmp_path),
            target="serverless",
            name=None,
            region="cn-beijing-6",
            account_id="2000003485",
            observability=True,
            no_cache=True,
            port=8000,
            namespace="default",
            registry=None,
            ks3_bucket=None,
            ks3_path="ks3://bucket/agents/demo-agent/code_manual.zip",
            image=None,
            ui_profile=None,
            ui_path=None,
            ui_url=None,
            dry_run=False,
            artifact_type="Code",
            no_version=True,
            auto_rollback=False,
        )
    )

    out = capsys.readouterr().out
    assert "已显式指定 --ks3-path" in out
    assert provider.calls == ["validate", "package", "deploy"]


def test_launch_cli_network_options_apply_to_deploy_target(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [
            str(tmp_path),
            "--ks3-path",
            "ks3://bucket/agents/demo-agent/code_manual.zip",
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
            "--no-version",
        ],
    )

    assert result.exit_code == 0, result.output
    assert provider.last_target is not None
    assert provider.last_target.network.enable_public_access is False
    assert provider.last_target.network.enable_vpc_access is True
    assert provider.last_target.network.vpc_id == "vpc-cli"
    assert provider.last_target.network.subnet_id == "subnet-cli"
    assert provider.last_target.network.security_group_id == "sg-cli"
    assert provider.last_target.network.availability_zone == "cn-beijing-6b"


def test_launch_network_ids_imply_vpc_access(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    runner = CliRunner()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [
            str(tmp_path),
            "--ks3-path",
            "ks3://bucket/agents/demo-agent/code_manual.zip",
            "--vpc-id",
            "vpc-cli",
            "--subnet-id",
            "subnet-cli",
            "--security-group-id",
            "sg-cli",
            "--no-version",
        ],
    )

    assert result.exit_code == 0, result.output
    assert provider.last_target is not None
    assert provider.last_target.network.enable_vpc_access is True


def test_launch_cli_forwards_explicit_env_and_env_file(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    runner = CliRunner()
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "APP_MODE=file\nFILE_ONLY=1\nOVERRIDE_ME=from-file\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_launch._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    result = runner.invoke(
        cmd_launch.launch,
        [
            str(tmp_path),
            "--ks3-path",
            "ks3://bucket/agents/demo-agent/code_manual.zip",
            "--env-file",
            str(env_file),
            "--env",
            "OVERRIDE_ME=from-cli",
            "--env",
            "CLI_ONLY=yes",
            "--no-version",
        ],
    )

    assert result.exit_code == 0, result.output
    assert provider.last_target is not None
    assert provider.last_target.extra["env_vars"] == {
        "APP_MODE": "file",
        "FILE_ONLY": "1",
        "OVERRIDE_ME": "from-cli",
        "CLI_ONLY": "yes",
    }


def test_launch_reads_ui_config_from_agentengine_yaml_when_cli_not_set(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr(
        "ksadk.cli.cmd_launch._load_config",
        lambda *_args, **_kwargs: {
            "name": "demo-agent",
            "ui": {
                "profile": "custom",
                "path": "/custom-chat",
                "url": "https://ui.example.com/custom-chat",
            },
        },
    )
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    asyncio.run(
        cmd_launch._launch_async(
            agent_dir=str(tmp_path),
            target="serverless",
            name=None,
            region="cn-beijing-6",
            account_id="2000003485",
            observability=True,
            no_cache=False,
            port=8000,
            namespace="default",
            registry=None,
            ks3_bucket=None,
            ks3_path="ks3://bucket/agents/demo-agent/code_manual.zip",
            image=None,
            ui_profile=None,
            ui_path=None,
            ui_url=None,
            dry_run=False,
            artifact_type="Code",
            no_version=True,
            auto_rollback=False,
        )
    )

    assert provider.last_target is not None
    assert provider.last_target.extra["ui_profile"] == "custom"
    assert provider.last_target.extra["ui_path"] == "/custom-chat"
    assert provider.last_target.extra["ui_url"] == "https://ui.example.com/custom-chat"
