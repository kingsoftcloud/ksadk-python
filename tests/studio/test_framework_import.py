"""Framework Manifest 真正导入（方案 §6.1）：检测 + 完整保留配置生成 Studio Draft。

验证：根 agentengine.yaml 声明 framework: langgraph 时，detect_importable_project 返回待导入信息；
commit_project 保留 Runtime/Prompt/Model/Task/Context 生成 AgentDraft；bootstrap 暴露 importableProject。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService


def _write_framework_workspace(tmp_path: Path, *, with_context: bool = False) -> None:
    """写根 agentengine.yaml（供 detect_importable_project 检测，方案 §6.1 根 manifest）。"""
    manifest = "framework: langgraph\nname: demo-agent\nmodel: glm-5.2\nprompt: 你是助手\nentry_point: src/agent.py\nagent_variable: graph\ntask: 先分析再改\n"
    if with_context:
        manifest += (
            "context:\n  maxInputTokens: 65536\n  reserveOutputTokens: 8192\n  ownership: ksadk\n"
        )
    (tmp_path / "agentengine.yaml").write_text(manifest, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "agent.py").write_text("graph = object()\n")


def _write_framework_project(tmp_path: Path, *, with_context: bool = False) -> Path:
    """写一个 langgraph 项目子目录（供 inspect_project 导入，避开 .agentkit digest）。"""
    project = tmp_path / "project"
    project.mkdir()
    lines = [
        "framework: langgraph",
        "name: demo-agent",
        "model: glm-5.2",
        "prompt: 你是助手",
        "entry_point: src/agent.py",
        "agent_variable: graph",
        "task: 先分析再改",
    ]
    if with_context:
        lines += [
            "context:",
            "  maxInputTokens: 65536",
            "  reserveOutputTokens: 8192",
            "  ownership: ksadk",
        ]
    (project / "agentengine.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    src = project / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "agent.py").write_text("graph = object()\n")
    return project


def test_detect_importable_project_returns_framework_info(tmp_path):
    _write_framework_workspace(tmp_path)
    svc = StudioService(tmp_path)
    result = svc.detect_importable_project()
    assert result is not None
    assert result["kind"] == "framework"
    assert result["runtimeType"] == "langgraph"
    assert result["name"] == "demo-agent"
    assert result["model"] == "glm-5.2"
    assert "你是助手" in result["prompt"]
    assert result["task"] == "先分析再改"
    assert result["requiresConfirmation"] is True


def test_detect_importable_project_none_for_codex_root(tmp_path):
    """根 codex manifest 不返回 framework 待导入（它是 codex agent，由 codex 路径处理）。"""
    (tmp_path / "agentengine.yaml").write_text(
        "name: codex-a\nframework: codex\nartifact_type: ManagedRuntime\nruntime:\n  name: codex\n  version: '0.144.4'\nmodel: m\nprompt: p\n",
        encoding="utf-8",
    )
    svc = StudioService(tmp_path)
    assert svc.detect_importable_project() is None


def test_detect_importable_project_none_when_no_manifest(tmp_path):
    svc = StudioService(tmp_path)
    assert svc.detect_importable_project() is None


def test_bootstrap_exposes_importable_project(tmp_path):
    _write_framework_workspace(tmp_path)
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/system/bootstrap").json()
        imp = bootstrap.get("importableProject")
        assert imp is not None
        assert imp["runtimeType"] == "langgraph"
        assert imp["name"] == "demo-agent"


def test_bootstrap_importable_project_none_when_no_framework(tmp_path):
    svc = StudioService(tmp_path)
    app = create_studio_app(tmp_path, service=svc, security_enabled=False)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/system/bootstrap").json()
        assert bootstrap.get("importableProject") is None


def test_commit_project_preserves_model_task_and_context(tmp_path):
    """commit_project 保留 model/task/context（方案 §6.1 完整保留配置）。"""
    _write_framework_project(tmp_path, with_context=True)
    svc = StudioService(tmp_path)
    inspection = svc.inspect_agent_project("project")
    draft = svc.commit_agent_project(
        inspection["inspectionToken"],
        name="demo-agent",
        slug="demo-agent",
    )
    assert draft.metadata.id.startswith("demo-agent")
    assert draft.spec.runtime.type == "langgraph"
    assert draft.spec.runtime.entry_point == "src/agent.py"
    assert draft.spec.runtime.agent_variable == "graph"
    assert "你是助手" in draft.spec.instructions.system
    assert draft.spec.instructions.task == "先分析再改"
    # model 保留
    assert draft.spec.model is not None
    assert draft.spec.model.model == "glm-5.2"
    # context 保留
    assert draft.spec.context.max_input_tokens == 65536
    assert draft.spec.context.reserve_output_tokens == 8192
    assert draft.spec.context.ownership == "ksadk"
    # source label
    assert draft.metadata.labels.get("agentkit.ksyun.com/source") == "project-detection"
    # list_agents 现在能列出
    agents = svc.list_agents()
    assert any(a.metadata.id.startswith("demo-agent") for a in agents)


def test_list_agents_shows_imported_framework_agent(tmp_path):
    """导入后 list_agents 包含 framework agent（不再空列表）。"""
    _write_framework_project(tmp_path)
    svc = StudioService(tmp_path)
    assert svc.list_agents() == []  # 导入前空
    inspection = svc.inspect_agent_project("project")
    svc.commit_agent_project(inspection["inspectionToken"], name="demo-agent", slug="demo-agent")
    agents = svc.list_agents()
    assert len(agents) == 1
    assert agents[0].metadata.id.startswith("demo-agent")
    assert agents[0].spec.runtime.type == "langgraph"


def test_commit_project_preserves_model_without_context(tmp_path):
    """无 context 声明时走默认 ContextSpec（方案 §13.1 兼容）。"""
    _write_framework_project(tmp_path, with_context=False)
    svc = StudioService(tmp_path)
    inspection = svc.inspect_agent_project("project")
    draft = svc.commit_agent_project(
        inspection["inspectionToken"], name="demo-agent", slug="demo-agent"
    )
    assert draft.spec.model is not None
    assert draft.spec.model.model == "glm-5.2"
    # context 走默认
    assert draft.spec.context.max_input_tokens == 32000  # ContextSpec 默认
    assert draft.spec.context.ownership == "auto"
