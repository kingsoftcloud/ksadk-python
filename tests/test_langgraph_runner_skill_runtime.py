from __future__ import annotations


def _tool_names(tools):
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


def test_langgraph_projects_can_bind_agentengine_toolsets_before_graph_compile(monkeypatch):
    from ksadk.toolsets import get_agentengine_tools

    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")

    user_tools = [lambda value: value]
    user_tools[0].__name__ = "custom_tool"

    tools = [*user_tools, *get_agentengine_tools(include=["skill"])]

    assert _tool_names(tools) == [
        "custom_tool",
        "list_skills",
        "search_skills",
        "load_skill",
        "execute_skills",
    ]
