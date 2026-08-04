"""Opt-in real Codex → Responses-to-Chat → Xingliu Studio E2E."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ksadk.cli.env_options import load_env_file
from ksadk.runtime import RuntimeExecutor, build_default_runtime_registry
from ksadk.studio.codex_builder import CodexStudioBuilder
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.codex_run import CodexRunSpecResolver
from ksadk.studio.contracts import RunStatus
from ksadk.studio.run_service import StudioRunService
from ksadk.studio.workspace import Workspace

_MODEL_ENV_KEYS = ("OPENAI_API_BASE", "OPENAI_API_KEY", "OPENAI_MODEL_NAME")
_E2E_ENV_FILE = Path.home() / "agentengine-test/0611agent-xiayu/.env"
_FIXTURE = Path(__file__).parent / "fixtures/review_workspace"


def _e2e_enabled() -> bool:
    return os.environ.get("KSADK_CODEX_E2E") == "1" and _E2E_ENV_FILE.is_file()


@pytest.mark.skipif(not _e2e_enabled(), reason="real Codex Xingliu E2E is opt-in")
@pytest.mark.asyncio
async def test_codex_studio_reviews_source_through_real_xingliu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = load_env_file(_E2E_ENV_FILE)
    for key in _MODEL_ENV_KEYS:
        value = values.get(key)
        assert value, f"{key} is required in the private E2E env file"
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "1")

    shutil.copytree(_FIXTURE / "src", tmp_path / "src")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    manifest = CodexAgentManifest.model_validate(
        {
            "name": "review-helper",
            "version": "1.0.0",
            "framework": "codex",
            "artifact_type": "ManagedRuntime",
            "runtime": {"name": "codex", "version": "0.144.4"},
            "model": "glm-5.2",
            "prompt": (
                "你是只读代码审查助手。必须先读取 src/demo.py，再报告一个确定、"
                "可复现的问题，并给出最小修复建议。不得修改文件。\n"
            ),
        }
    )
    snapshot = CodexManifestRepository(workspace).save(manifest)
    build = CodexStudioBuilder(workspace).build()
    run_service = StudioRunService(
        workspace,
        RuntimeExecutor(build_default_runtime_registry()),
    )
    run = await run_service.run(
        CodexRunSpecResolver(workspace).resolve(build.id),
        "请读取 src/demo.py，指出一个确定的问题和最小修复建议。",
        session_id="ses-real-xingliu-e2e",
    )

    assert run.status == RunStatus.COMPLETED, run.error
    assert run.output
    assert run.manifest_sha256 == snapshot.manifest_sha256
    events = run_service.event_store.events(run.id)
    event_types = [event.type for event in events]
    assert "proxy.requested" in event_types
    assert "proxy.upstream" in event_types
    assert "command.started" in event_types
    assert "command.completed" in event_types
    assert "run.completed" in event_types
    command = next(event for event in events if event.type == "command.started")
    assert "src/demo.py" in str(command.data)

    secret = values["OPENAI_API_KEY"].encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), f"API key leaked into {path}"
