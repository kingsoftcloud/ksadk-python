import asyncio
from pathlib import Path

from ksadk.cli import cmd_deploy
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

    async def validate_config(self, _target):
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

    async def deploy(self, package_info, _target):
        self.calls.append("deploy")
        assert package_info.metadata.get("ks3_path")
        return DeployResult(
            status=DeployStatus.DEPLOYING,
            agent_id="ar-demo",
            agent_name="demo-agent",
            endpoint="http://demo-endpoint",
            message="ok",
        )


def test_deploy_no_cache_triggers_build_and_clears_metadata(tmp_path: Path, monkeypatch):
    provider = _FakeProvider()
    metadata_dir = tmp_path / ".agentengine"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "build-metadata.json").write_text('{"metadata":{"ks3_path":"ks3://old/path.zip"}}', encoding="utf-8")

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    asyncio.run(
        cmd_deploy._deploy_async(
            agent_dir=str(tmp_path),
            target="serverless",
            name=None,
            region="cn-beijing-6",
            account_id="2000003485",
            artifact_type="Code",
            namespace="default",
            port=8000,
            registry=None,
            ks3_path=None,
            ks3_bucket=None,
            image=None,
            ui_profile=None,
            ui_path=None,
            ui_url=None,
            observability=True,
            push=False,
            no_cache=True,
            no_version=True,
            auto_rollback=False,
            dry_run=False,
        )
    )

    assert provider.package_metadata_file_exists is False
    assert provider.calls == ["validate", "package", "build", "deploy"]


def test_deploy_no_cache_warns_when_explicit_ks3_path_is_supplied(tmp_path: Path, monkeypatch, capsys):
    provider = _FakeProvider()

    monkeypatch.setattr("ksadk.detection.FrameworkDetector", lambda *_args, **_kwargs: type("D", (), {"detect": lambda self: _FakeDetectionResult()})())
    monkeypatch.setattr("ksadk.cli.cmd_deploy._load_config", lambda *_args, **_kwargs: {"name": "demo-agent"})
    monkeypatch.setattr("ksadk.deployment.DeploymentManager.get_provider", lambda *_args, **_kwargs: provider)

    asyncio.run(
        cmd_deploy._deploy_async(
            agent_dir=str(tmp_path),
            target="serverless",
            name=None,
            region="cn-beijing-6",
            account_id="2000003485",
            artifact_type="Code",
            namespace="default",
            port=8000,
            registry=None,
            ks3_path="ks3://bucket/agents/demo-agent/code_manual.zip",
            ks3_bucket=None,
            image=None,
            ui_profile=None,
            ui_path=None,
            ui_url=None,
            observability=True,
            push=False,
            no_cache=True,
            no_version=True,
            auto_rollback=False,
            dry_run=False,
        )
    )

    out = capsys.readouterr().out
    assert "已显式指定 --ks3-path" in out
    assert provider.calls == ["validate", "package", "deploy"]
