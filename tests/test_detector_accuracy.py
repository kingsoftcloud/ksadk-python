"""框架检测器识别准确率回归测试。

覆盖 detector.py 收紧为 AST 调用级判定后的塌缩修复场景：
- deepagents / langchain create_agent / langgraph StateGraph / LCEL legacy / create_react_agent
- 注释含 "StateGraph" 不再误判 langgraph
- 混合高层+底层编排时高层优先
- AST 解析失败走字符串兜底
- cmd_a2a langchain 映射到 LangGraphRuntimeAdapter
"""

from __future__ import annotations

from pathlib import Path

from ksadk.detection import FrameworkDetector, FrameworkType


def _write_script_agent(project_dir: Path, content: str, entry: str = "agent.py") -> Path:
    """脚本式项目：根目录直接放 entry 文件，无 __init__.py、无 config，强制走 _analyze_code。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / entry).write_text(content, encoding="utf-8")
    return project_dir


def _detect(project_dir: Path) -> FrameworkType:
    return FrameworkDetector(str(project_dir)).detect().type


def test_deepagents_script_detected_without_config(tmp_path: Path):
    _write_script_agent(
        tmp_path,
        "from deepagents import create_deep_agent\nroot_agent = create_deep_agent(model=None)\n",
    )
    assert _detect(tmp_path) == FrameworkType.DEEPAGENTS


def test_langchain_create_agent_not_collapsed_to_langgraph(tmp_path: Path):
    """新版 langchain create_agent 底层是 langgraph 图，但应判 LANGCHAIN（修复塌缩核心）。"""
    _write_script_agent(
        tmp_path,
        "from langchain.agents import create_agent\n"
        "from langchain_openai import ChatOpenAI\n"
        "root_agent = create_agent(model=ChatOpenAI(), tools=[])\n",
    )
    result = FrameworkDetector(str(tmp_path)).detect()
    assert result.type == FrameworkType.LANGCHAIN
    assert result.confidence == 0.9


def test_langgraph_stategraph_with_langchain_import_still_langgraph(tmp_path: Path):
    """langgraph 直接编排 StateGraph 即使同时 import langchain_openai 也应判 LANGGRAPH。"""
    _write_script_agent(
        tmp_path,
        "from langchain_openai import ChatOpenAI\n"
        "from langgraph.graph import StateGraph, END\n"
        "graph = StateGraph(dict)\n"
        "graph.add_node('chat', lambda s: s)\n"
        "root_agent = graph.compile()\n",
    )
    assert _detect(tmp_path) == FrameworkType.LANGGRAPH


def test_lcel_chain_detected_as_langchain_legacy(tmp_path: Path):
    """legacy LCEL 链无高层 API、无 langgraph 编排，走 legacy 兜底判 LANGCHAIN。"""
    _write_script_agent(
        tmp_path,
        "from langchain_openai import ChatOpenAI\n"
        "from langchain_core.output_parsers import StrOutputParser\n"
        "root_agent = ChatOpenAI() | StrOutputParser()\n",
    )
    result = FrameworkDetector(str(tmp_path)).detect()
    assert result.type == FrameworkType.LANGCHAIN
    assert result.confidence == 0.8


def test_create_react_agent_detected_as_langgraph(tmp_path: Path):
    """create_react_agent 现仅 langgraph.prebuilt 导出，调用即判 LANGGRAPH（不做来源区分）。"""
    _write_script_agent(
        tmp_path,
        "from langgraph.prebuilt import create_react_agent\n"
        "from langchain_openai import ChatOpenAI\n"
        "root_agent = create_react_agent(ChatOpenAI(), tools=[])\n",
    )
    assert _detect(tmp_path) == FrameworkType.LANGGRAPH


def test_stategraph_in_comment_does_not_trigger_langgraph(tmp_path: Path):
    """注释里出现 "StateGraph" 字符串不再误判 langgraph（修复字符串包含塌缩）。"""
    _write_script_agent(
        tmp_path,
        "from langchain_openai import ChatOpenAI\n"
        "from langchain_core.output_parsers import StrOutputParser\n"
        "# 这里用 StateGraph 只是个注释，实际是 LCEL 链\n"
        "root_agent = ChatOpenAI() | StrOutputParser()\n",
    )
    assert _detect(tmp_path) == FrameworkType.LANGCHAIN


def test_high_level_create_agent_wins_over_stategraph_in_mixed_code(tmp_path: Path):
    """同时出现 create_agent 调用与 StateGraph 调用时，高层 API 优先判 LANGCHAIN。"""
    _write_script_agent(
        tmp_path,
        "from langchain.agents import create_agent\n"
        "from langgraph.graph import StateGraph\n"
        "from langchain_openai import ChatOpenAI\n"
        "root_agent = create_agent(model=ChatOpenAI(), tools=[])\n"
        "unused_graph = StateGraph(dict)\n",
    )
    assert _detect(tmp_path) == FrameworkType.LANGCHAIN


def test_syntax_error_falls_back_to_string_classification(tmp_path: Path):
    """AST 解析失败时不抛异常，退化到字符串兜底判定。"""
    _write_script_agent(
        tmp_path,
        "from langchain_openai import ChatOpenAI\nroot_agent =\n",  # 语法错误（不完整赋值）
    )
    assert _detect(tmp_path) == FrameworkType.LANGCHAIN


def test_adk_llm_agent_detected_without_config(tmp_path: Path):
    _write_script_agent(
        tmp_path,
        "from google.adk.agents import LlmAgent\nroot_agent = LlmAgent(name='a', model='m')\n",
    )
    assert _detect(tmp_path) == FrameworkType.ADK


def test_cmd_a2a_normalizes_langchain_family_to_langgraph_runtime_adapter(monkeypatch, tmp_path):
    """LangChain/DeepAgents 共享 LangGraph RuntimeAdapter，不另造顶层 runtime。"""
    import ksadk.cli.cmd_a2a as mod

    selected: list[str] = []
    monkeypatch.setattr(mod, "_setup_tracing", lambda _runtime_type: None)
    monkeypatch.setattr(
        mod,
        "create_runtime_adapter",
        lambda context: selected.append(context.runtime_type) or object(),
    )

    expected_runtime_types = (
        ("langchain", "langgraph"),
        ("deepagents", "langgraph"),
        ("adk", "adk"),
    )
    for framework, expected in expected_runtime_types:
        detection_type = type("Type", (), {"value": framework})()
        detection = type("Detection", (), {"type": detection_type, "raw_config": {}})()
        monkeypatch.setattr(mod, "_detect_project", lambda _path, value=detection: value)
        mod._load_runtime_adapter(tmp_path, no_trace=True)
        assert selected.pop() == expected


def test_codex_yaml_detected(tmp_path: Path):
    """fallback:项目根有 codex.yaml 即判 CODEX。"""
    (tmp_path / "codex.yaml").write_text("model: glm-5.2\n", encoding="utf-8")
    assert _detect(tmp_path) == FrameworkType.CODEX


def test_codex_framework_in_ksadk_yaml(tmp_path: Path):
    """显式:ksadk.yaml framework: codex(无需 agent.py)。"""
    (tmp_path / "ksadk.yaml").write_text(
        "framework: codex\nname: my-codex-agent\n", encoding="utf-8"
    )
    assert _detect(tmp_path) == FrameworkType.CODEX
