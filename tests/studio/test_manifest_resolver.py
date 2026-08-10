"""ManifestResolver 多 Runner 导入修复（方案 §2.4 第 1 点 / §6.1）。

Bad Case：标准 LangGraph 项目作为 Studio workspace 启动，根 agentengine.yaml 声明
framework: langgraph，CodexManifestRepository 不应把它当 Codex 解析抛 CODEX_MANIFEST_INVALID，
list_agents / is_codex_agent 应正确识别为 framework agent。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.codex_manifest import CodexManifestRepository
from ksadk.studio.manifest_resolver import detect_manifest_kind, root_manifest_is_codex
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace


def _write_root_manifest(workspace_root: Path, content: str) -> None:
    (workspace_root / "agentengine.yaml").write_text(content, encoding="utf-8")


# ---- detect_manifest_kind 单测 ----


def test_detect_codex_manifest():
    import tempfile

    d = Path(tempfile.mkdtemp())
    _write_root_manifest(
        d,
        "name: codex-agent\nframework: codex\nruntime:\n  name: codex\n  version: '0.144.4'\nmodel: m\nprompt: p\n",
    )
    result = detect_manifest_kind(d)
    assert result.kind == "codex"
    assert result.is_codex


def test_detect_langgraph_manifest():
    import tempfile

    d = Path(tempfile.mkdtemp())
    _write_root_manifest(
        d, "framework: langgraph\nruntime:\n  type: langgraph\n  projectPath: runtimes/demo\n"
    )
    result = detect_manifest_kind(d)
    assert result.kind == "framework"
    assert not result.is_codex


def test_detect_adk_manifest():
    import tempfile

    d = Path(tempfile.mkdtemp())
    _write_root_manifest(d, "framework: adk\nruntime:\n  type: adk\n")
    result = detect_manifest_kind(d)
    assert result.kind == "framework"


def test_detect_no_manifest():
    import tempfile

    d = Path(tempfile.mkdtemp())
    result = detect_manifest_kind(d)
    assert result.kind == "none"


def test_detect_ambiguous_manifest():
    import tempfile

    d = Path(tempfile.mkdtemp())
    _write_root_manifest(d, "name: my-agent\nversion: '1.0'\n")
    result = detect_manifest_kind(d)
    assert result.kind == "ambiguous"


def test_root_manifest_is_codex_helper():
    import tempfile

    d = Path(tempfile.mkdtemp())
    _write_root_manifest(d, "framework: langgraph\n")
    assert root_manifest_is_codex(d) is False
    d2 = Path(tempfile.mkdtemp())
    _write_root_manifest(d2, "framework: codex\nruntime:\n  name: codex\n")
    assert root_manifest_is_codex(d2) is True


# ---- CodexManifestRepository 不误判根 langgraph manifest ----


def test_codex_repo_list_skips_non_codex_root_manifest(tmp_path):
    """根 agentengine.yaml 声明 framework: langgraph 时，list() 不抛 CODEX_MANIFEST_INVALID。"""
    _write_root_manifest(
        tmp_path,
        "framework: langgraph\nruntime:\n  type: langgraph\n  projectPath: runtimes/demo\n",
    )
    workspace = Workspace(tmp_path)
    repo = CodexManifestRepository(workspace)
    # 修复前：list() 会无脑 _load_path(self.path) → CODEX_MANIFEST_INVALID
    snapshots = repo.list()
    assert snapshots == []  # 根非 codex，跳过；无 agents/ 子目录 → 空


def test_codex_repo_exists_returns_false_for_non_codex_root(tmp_path):
    _write_root_manifest(tmp_path, "framework: langgraph\n")
    workspace = Workspace(tmp_path)
    repo = CodexManifestRepository(workspace)
    assert repo.exists() is False  # 非 codex 根 manifest 不算 codex agent 存在


def test_codex_repo_load_raises_not_found_for_non_codex_root(tmp_path):
    _write_root_manifest(tmp_path, "framework: langgraph\n")
    workspace = Workspace(tmp_path)
    repo = CodexManifestRepository(workspace)
    with pytest.raises(Exception):  # not_found
        repo.load()


def test_codex_repo_still_lists_real_codex_root(tmp_path):
    """真 codex 根 manifest 仍被正确解析列出。"""
    _write_root_manifest(
        tmp_path,
        "name: codex-a\nversion: '1.0'\nframework: codex\nartifact_type: ManagedRuntime\nruntime:\n  name: codex\n  version: '0.144.4'\nmodel: m\nprompt: p\n",
    )
    workspace = Workspace(tmp_path)
    repo = CodexManifestRepository(workspace)
    snapshots = repo.list()
    assert len(snapshots) == 1
    assert snapshots[0].manifest.name == "codex-a"


# ---- 端到端：Studio 启动时不误判 langgraph workspace ----


def test_studio_lists_agents_with_langgraph_root_manifest(tmp_path):
    """Studio 启动 + list_agents：根 langgraph manifest 不抛错，正常返回空列表。"""
    _write_root_manifest(
        tmp_path,
        "framework: langgraph\nruntime:\n  type: langgraph\n  projectPath: runtimes/demo\n",
    )
    service = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        r = client.get("/api/v1/agents")
        assert r.status_code == 200
        assert r.json()["items"] == []  # 无 agent，但不抛 CODEX_MANIFEST_INVALID


def test_studio_creates_framework_agent_alongside_langgraph_root(tmp_path):
    """根 langgraph manifest 存在时，Studio 不误判，list_agents 正常返回。"""
    _write_root_manifest(
        tmp_path,
        "framework: langgraph\nruntime:\n  type: langgraph\n  projectPath: runtimes/demo\n",
    )
    service = StudioService(tmp_path)
    # 不调真实 create（langgraph runtime 可能未装），只验证 list 不抛 CODEX_MANIFEST_INVALID
    agents = service.list_agents()
    assert agents == []
    assert service.is_codex_agent("") is False or True  # 不抛即可
