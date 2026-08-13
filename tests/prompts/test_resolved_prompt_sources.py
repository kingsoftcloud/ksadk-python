"""PR A：ResolvedPromptSources / PlatformPolicySource / compile_resolved_prompt_dict 单测。"""

from __future__ import annotations

import pytest

from ksadk.prompts.resolved import (
    RESOLVED_PROMPT_SOURCES_VERSION,
    EnvPlatformPolicySource,
    ResolvedPromptSources,
    compile_resolved_prompt_dict,
    get_default_platform_policy_source,
    sections_from_resolved_sources,
)


def test_resolved_prompt_sources_version() -> None:
    assert RESOLVED_PROMPT_SOURCES_VERSION == "v1"


def test_env_platform_policy_source_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    assert EnvPlatformPolicySource().resolve() is None


def test_env_platform_policy_source_set_returns_stripped_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KSADK_PLATFORM_SAFETY_TEXT", "  平台安全规则  ")
    src = EnvPlatformPolicySource()
    assert src.resolve() == "平台安全规则"
    assert src.source == "env_local_dev"
    assert src.version == "env"


def test_get_default_platform_policy_source_returns_env_source() -> None:
    src = get_default_platform_policy_source()
    assert src is not None
    assert isinstance(src, EnvPlatformPolicySource)


def test_sections_from_resolved_sources_all_empty_returns_empty() -> None:
    assert sections_from_resolved_sources(ResolvedPromptSources()) == []


def test_sections_from_resolved_sources_partial_no_platform_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    sections = sections_from_resolved_sources(
        ResolvedPromptSources(
            agent_system="你是助手", agent_task="用中文", request_instructions="hi"
        )
    )
    kinds = [s.kind for s in sections]
    # canonical 顺序：agent_identity(20) → agent_policy(30) → request_instructions(60)
    assert kinds == ["agent_identity", "agent_policy", "request_instructions"]
    # env 未设 → 无 platform_safety
    assert "platform_safety" not in kinds


def test_sections_from_resolved_sources_with_platform_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KSADK_PLATFORM_SAFETY_TEXT", "平台安全规则")
    sections = sections_from_resolved_sources(
        ResolvedPromptSources(
            agent_system="你是助手",
            agent_task="用中文",
            request_instructions="hi",
            platform_policy_source=EnvPlatformPolicySource(),
        )
    )
    kinds = [s.kind for s in sections]
    assert "platform_safety" in kinds
    safety = next(s for s in sections if s.kind == "platform_safety")
    assert safety.content == "平台安全规则"
    assert safety.source == "env_local_dev"
    assert safety.merge_policy == "protected"


def test_compile_resolved_prompt_dict_all_empty_returns_none() -> None:
    assert compile_resolved_prompt_dict(ResolvedPromptSources()) is None


def test_compile_resolved_prompt_dict_non_empty_has_all_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    d = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手", agent_task="用中文", request_instructions="hi"
        )
    )
    assert d is not None
    assert d["prompt_content_hash"].startswith("sha256:")
    # agent_system/agent_task 是 stable → stable_prefix_hash 非空。
    assert d["prompt_stable_prefix_hash"].startswith("sha256:")
    assert set(d["prompt_section_hashes"]) == {
        "agent_identity",
        "agent_policy",
        "request_instructions",
    }
    assert d["prompt_section_count"] == 3
    assert d["prompt_resolved_sources_version"] == "v1"
    # env 未设 → 无 platform_policy_version
    assert d["prompt_platform_policy_version"] is None
    assert d["prompt_platform_policy_source"] is None


def test_compile_resolved_prompt_dict_with_platform_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_PLATFORM_SAFETY_TEXT", "平台安全规则")
    d = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手",
            platform_policy_source=EnvPlatformPolicySource(),
        )
    )
    assert d is not None
    assert "platform_safety" in d["prompt_section_hashes"]
    assert d["prompt_platform_policy_version"] == "env"
    assert d["prompt_platform_policy_source"] == "env_local_dev"


def test_compile_resolved_prompt_dict_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    sources = ResolvedPromptSources(
        agent_system="你是助手", agent_task="用中文", request_instructions="hi"
    )
    a = compile_resolved_prompt_dict(sources)
    b = compile_resolved_prompt_dict(sources)
    assert a is not None and b is not None
    assert a["prompt_content_hash"] == b["prompt_content_hash"]
    assert a["prompt_stable_prefix_hash"] == b["prompt_stable_prefix_hash"]


def test_stable_prefix_hash_excludes_request_instructions_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_instructions(volatile) 变化不改变 stable_prefix_hash（agent_system/task 不变）。"""
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    base = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手", agent_task="用中文", request_instructions="问题A"
        )
    )
    changed = compile_resolved_prompt_dict(
        ResolvedPromptSources(
            agent_system="你是助手", agent_task="用中文", request_instructions="问题B"
        )
    )
    assert base is not None and changed is not None
    assert base["prompt_stable_prefix_hash"] == changed["prompt_stable_prefix_hash"]
    assert base["prompt_content_hash"] != changed["prompt_content_hash"]  # canonical 内容确实变了
