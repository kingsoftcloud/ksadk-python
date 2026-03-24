import asyncio
from pathlib import Path

import pytest

from ksadk.api.client import DryRunExit
from ksadk.cli import cmd_mcp


class _FakeMCPDetectionResult:
    is_valid = True
    entry_point = "server.py"
    mcp_variable = "mcp"
    tools = ["tool_a", "tool_b"]


class _FakeMCPDetector:
    def __init__(self, *_args, **_kwargs):
        pass

    def detect(self):
        return _FakeMCPDetectionResult()


class _FakeDryRunClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def create_mcp(self, request):
        raise DryRunExit("dry-run", payload={"body": request})

    async def update_mcp(self, *_args, **_kwargs):
        raise DryRunExit("dry-run", payload={"body": {}})

    async def close(self):
        return None


def test_mcp_deploy_dry_run_skips_local_build_and_relies_on_plan(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ksadk.detection.mcp_detector.MCPDetector", _FakeMCPDetector)
    monkeypatch.setattr("ksadk.api.AgentEngineClient", _FakeDryRunClient)

    def _should_not_build(*_args, **_kwargs):
        raise AssertionError("Dry run should not trigger artifact build")

    monkeypatch.setattr(cmd_mcp, "_build_code_artifact", _should_not_build)

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

    payload = exc_info.value.payload or {}
    body = payload.get("body") or {}

    assert body["artifact_type"] == "Code"
    assert body["region"] == "cn-beijing-6"
    assert body["artifact_path"].startswith("ks3://agentengine-test/")
    assert "dry-run" in body["artifact_path"]
