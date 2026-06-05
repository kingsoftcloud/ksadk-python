from __future__ import annotations


def _tool_names(tools):
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


def test_deepagents_projects_can_use_agentengine_toolsets_before_compile(monkeypatch):
    from ksadk.runners.deepagents_runner import DeepAgentsRunner
    from ksadk.runners.langgraph_runner import LangGraphRunner
    from ksadk.toolsets import get_agentengine_tools

    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.delenv("KSADK_SANDBOX_TEMPLATE_ID", raising=False)

    tools = get_agentengine_tools(include=["skill", "workspace", "platform", "sandbox"])
    names = _tool_names(tools)

    assert issubclass(DeepAgentsRunner, LangGraphRunner)
    assert "execute_skills" in names
    assert "workspace_status" in names
    assert "component_status" in names
    assert "sandbox_status" in names
