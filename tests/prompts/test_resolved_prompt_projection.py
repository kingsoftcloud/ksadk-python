"""PR B：compile_resolved_prompt_dict 含 prompt_content==compiled.content，空来源返回 None。"""

from __future__ import annotations

from ksadk.prompts.compiler import PromptCompiler
from ksadk.prompts.resolved import (
    ResolvedPromptSources,
    compile_resolved_prompt_dict,
    sections_from_resolved_sources,
)


def test_dict_carries_prompt_content_equal_to_compiled_content(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    sources = ResolvedPromptSources(
        agent_system="你是助手",
        agent_task="用中文简短回答",
        request_instructions="本次问题：介绍 GIL",
    )
    compiled_dict = compile_resolved_prompt_dict(sources)
    assert compiled_dict is not None
    # prompt_content 必须等于真实编译正文
    sections = sections_from_resolved_sources(sources)
    expected_content = PromptCompiler().compile(sections).content
    assert compiled_dict["prompt_content"] == expected_content
    # 正文含 XML 区段标签
    assert "<agent_identity>" in expected_content
    assert "<agent_policy>" in expected_content
    assert "<request_instructions>" in expected_content


def test_empty_sources_return_none(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    assert compile_resolved_prompt_dict(ResolvedPromptSources()) is None


def test_request_instructions_only_still_carries_content(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    compiled_dict = compile_resolved_prompt_dict(
        ResolvedPromptSources(request_instructions="只本轮指令")
    )
    assert compiled_dict is not None
    assert compiled_dict["prompt_content"]
    assert "<request_instructions>" in compiled_dict["prompt_content"]
