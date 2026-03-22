import asyncio
from pathlib import Path

import pytest

from ksadk.api.client import DryRunExit
from ksadk.builders.base import BuildResult
from ksadk.cli import cmd_mcp


class _FakeNow:
    def strftime(self, _fmt: str) -> str:
        return "20260320173045"


class _FakeDatetime:
    @staticmethod
    def now():
        return _FakeNow()


class _FakeDetectionResult:
    is_valid = True
    name = "demo-mcp"
    entry_point = "server.py"
    mcp_variable = "mcp"
    tools = ["tool_a", "tool_b"]


class _FakeMCPDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def detect(self):
        return _FakeDetectionResult()


class _FakeMCPCodeBuilder:
    last_config = None

    def __init__(self, project_dir: Path, config: dict = None):
        self.project_dir = Path(project_dir)
        self.config = config or {}
        self.__class__.last_config = self.config

    def build(self) -> BuildResult:
        return BuildResult(
            success=True,
            artifact_path=self.project_dir / ".agentengine" / "mcp_build" / "demo_mcp.zip",
            artifact_size=1234,
            metadata={"mcp_name": "demo_mcp"},
        )


class _FakeKS3Uploader:
    last_object_key = None

    def __init__(self, region: str, bucket: str = None):
        self.region = region
        self.bucket = bucket

    async def upload(self, _file_path: Path, object_key: str):
        self.__class__.last_object_key = object_key
        return f"ks3://agentengine-test/{object_key}"


def test_mcp_deploy_no_cache_passes_builder_flag_and_uses_unique_object_key(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr("ksadk.builders.mcp_builder.MCPCodeBuilder", _FakeMCPCodeBuilder)
    monkeypatch.setattr("ksadk.builders.ks3_uploader.KS3Uploader", _FakeKS3Uploader)
    monkeypatch.setattr(cmd_mcp, "datetime", _FakeDatetime)

    with pytest.raises(DryRunExit) as exc_info:
        asyncio.run(
            cmd_mcp._deploy_mcp_async(
                mcp_dir=str(tmp_path),
                name=None,
                region="cn-beijing-6",
                ks3_bucket="agentengine-test",
                enable_auth=False,
                dry_run=True,
                artifact_type="Code",
                no_cache=True,
            )
        )
    expected_name = "code_20260320173045.zip"
    expected_key = f"mcps/{tmp_path.name.replace('-', '_').replace('.', '_')}/{expected_name}"
    payload = exc_info.value.payload or {}
    body = payload.get("body") or {}

    assert _FakeMCPCodeBuilder.last_config == {"no_cache": True}
    assert _FakeKS3Uploader.last_object_key == expected_key
    assert body["ArtifactPath"].endswith(expected_name)
    assert "CreateMCP" in payload.get("curl", "")
