"""PR B：CompiledPrompt.content XML 注入后，LangGraphRunner._to_state 产单个 SystemMessage，正文==XML。"""

from __future__ import annotations

from types import SimpleNamespace

from ksadk.prompts.resolved import ResolvedPromptSources, compile_resolved_prompt_dict
from ksadk.runners.langgraph_runner import LangGraphRunner


def _make_runner() -> LangGraphRunner:
    det = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    return LangGraphRunner(det, ".")


def test_compiled_prompt_xml_yields_single_system_message(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    compiled = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手",
            agent_task="用中文",
            request_instructions="本轮：介绍 GIL",
        )
    )
    assert compiled is not None
    xml = compiled["prompt_content"]

    runner = _make_runner()
    payload = {"input": "介绍 GIL", "instructions": xml, "history": []}
    state = runner._to_state(payload, [])

    messages = state["messages"]
    system_messages = [m for m in messages if m.__class__.__name__ == "SystemMessage"]
    # 恰好一个 SystemMessage，正文 == CompiledPrompt.content（XML）
    assert len(system_messages) == 1
    assert system_messages[0].content == xml
    # XML 区段标签可见
    assert "<agent_identity>" in system_messages[0].content
    assert "<agent_policy>" in system_messages[0].content
    assert "<request_instructions>" in system_messages[0].content


def test_empty_instructions_no_system_message(monkeypatch) -> None:
    """framework 回退/无 instructions 时不产 SystemMessage（结构不变）。"""
    runner = _make_runner()
    state = runner._to_state({"input": "hi", "history": []}, [])
    system_messages = [m for m in state["messages"] if m.__class__.__name__ == "SystemMessage"]
    assert system_messages == []


def test_runtime_placeholders_do_not_enter_langgraph_model_history() -> None:
    runner = _make_runner()
    history = [
        {"role": "model", "content": "before"},
        {"role": "model", "content": "[tool_call] dangerous()"},
        {"role": "user", "content": "[tool_result] result"},
        {"role": "model", "content": "[approval_request] approve?"},
        {"role": "user", "content": "[approval_response] approved"},
        {"role": "model", "content": "after"},
    ]

    state = runner._to_state({"input": "continue"}, history)

    contents = [message.content for message in state["messages"]]
    assert contents == ["before", "after", "continue"]


def test_square_bracket_text_is_not_corrupted_by_output_filter() -> None:
    runner = _make_runner()
    content = "normal [tool_call] documentation [approval_request] example"

    assert runner._filter_tool_tags(content) == content
