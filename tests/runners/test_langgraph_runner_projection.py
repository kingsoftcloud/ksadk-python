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
