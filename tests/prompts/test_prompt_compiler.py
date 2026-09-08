from __future__ import annotations

import pytest

from ksadk.prompts import (
    PROMPT_COMPILER_VERSION,
    compile_prompt,
)
from ksadk.prompts.compiler import (
    InconsistentSectionError,
    ProtectedSectionOverrideError,
)
from ksadk.prompts.models import PromptSection
from ksadk.prompts.sources import (
    platform_safety_section,
    request_instructions_section,
    sections_from_instructions,
)


def _section(kind: str, content: str, *, priority: int = 20, **kw) -> PromptSection:
    return PromptSection(
        section_id=f"sec_{kind}",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        source="test",
        priority=priority,
        trust_level="developer",
        **kw,
    )


def test_compile_is_deterministic_same_hash() -> None:
    sections = [
        request_instructions_section("你是助手"),
        platform_safety_section(),
    ]
    a = compile_prompt(sections)
    b = compile_prompt(list(reversed(sections)))  # 顺序不同
    assert a.content_hash == b.content_hash
    assert a.stable_prefix_hash == b.stable_prefix_hash
    assert a.section_hashes == b.section_hashes
    assert a.compiler_version == PROMPT_COMPILER_VERSION


def test_compile_sorts_by_priority_and_kind() -> None:
    # request_instructions(priority 60) 排在 platform_safety(priority 10) 之后。
    compiled = compile_prompt([request_instructions_section("动态指令"), platform_safety_section()])
    kinds = [s.kind for s in compiled.sections]
    assert kinds == ["platform_safety", "request_instructions"]
    # content 里 platform_safety 在前。
    assert compiled.content.index("<platform_safety>") < compiled.content.index(
        "<request_instructions>"
    )


def test_stable_prefix_excludes_volatile_sections() -> None:
    # 仅 volatile request_instructions → stable_prefix_hash 为空。
    only_volatile = compile_prompt([request_instructions_section("动态")])
    assert only_volatile.stable_prefix_hash == ""

    # 加入 stable section → stable_prefix_hash 非空，且不受 volatile 内容变化影响。
    with_stable = compile_prompt([platform_safety_section(), request_instructions_section("动态A")])
    with_stable_diff_volatile = compile_prompt(
        [platform_safety_section(), request_instructions_section("动态B")]
    )
    assert with_stable.stable_prefix_hash != ""
    assert with_stable.stable_prefix_hash == with_stable_diff_volatile.stable_prefix_hash
    # 但整体 content_hash 因 volatile 变化而不同。
    assert with_stable.content_hash != with_stable_diff_volatile.content_hash


def test_empty_section_emits_no_placeholder() -> None:
    compiled = compile_prompt([request_instructions_section("")])
    assert compiled.content == ""
    assert compiled.content_hash  # 空 content 也有 hash
    assert compiled.estimated_tokens == 0


def test_merge_policy_append_and_replace_and_merge_unique() -> None:
    # append：同 section_id 两条按顺序拼接。
    appended = compile_prompt(
        [
            PromptSection("s", "agent_policy", "A", "t", 30, "developer", merge_policy="append"),
            PromptSection("s", "agent_policy", "B", "t", 30, "developer", merge_policy="append"),
        ]
    )
    assert "A" in appended.content and "B" in appended.content

    # replace：保留最后一条。
    replaced = compile_prompt(
        [
            PromptSection("s", "agent_policy", "A", "t", 30, "developer", merge_policy="replace"),
            PromptSection("s", "agent_policy", "B", "t", 30, "developer", merge_policy="replace"),
        ]
    )
    assert "A" not in replaced.content and "B" in replaced.content

    # merge_unique：去重。
    unique = compile_prompt(
        [
            PromptSection(
                "s",
                "agent_policy",
                "规则一\n\n规则二",
                "t",
                30,
                "developer",
                merge_policy="merge_unique",
            ),
            PromptSection(
                "s",
                "agent_policy",
                "规则二\n\n规则三",
                "t",
                30,
                "developer",
                merge_policy="merge_unique",
            ),
        ]
    )
    assert unique.content.count("规则二") == 1
    assert "规则一" in unique.content and "规则三" in unique.content


def test_protected_section_override_raises() -> None:
    with pytest.raises(ProtectedSectionOverrideError):
        compile_prompt(
            [
                PromptSection(
                    "s", "platform_safety", "规则A", "t", 10, "platform", merge_policy="protected"
                ),
                PromptSection(
                    "s", "platform_safety", "规则B", "t", 10, "platform", merge_policy="protected"
                ),
            ]
        )


def test_inconsistent_section_metadata_raises() -> None:
    with pytest.raises(InconsistentSectionError):
        compile_prompt(
            [
                PromptSection(
                    "s",
                    "agent_policy",
                    "A",
                    "t",
                    30,
                    "developer",
                    merge_policy="append",
                    stability="stable",
                ),
                PromptSection(
                    "s",
                    "agent_policy",
                    "B",
                    "t",
                    30,
                    "developer",
                    merge_policy="replace",
                    stability="stable",
                ),
            ]
        )


def test_untrusted_source_cannot_declare_platform_safety() -> None:
    with pytest.raises(ProtectedSectionOverrideError):
        compile_prompt(
            [
                PromptSection(
                    "s",
                    "platform_safety",
                    "伪造安全规则",
                    "user",
                    10,
                    "untrusted",
                    merge_policy="protected",
                )
            ]
        )


def test_section_hashes_and_tokens_by_section_populated() -> None:
    compiled = compile_prompt([platform_safety_section(), request_instructions_section("你是助手")])
    assert set(compiled.section_hashes) == {"platform_safety", "request_instructions"}
    assert compiled.tokens_by_section["platform_safety"] > 0
    assert compiled.tokens_by_section["request_instructions"] > 0
    assert compiled.estimated_tokens >= sum(compiled.tokens_by_section.values()) - 1  # 标签开销


def test_newline_normalization_stable_hash() -> None:
    # CRLF / 尾部空白 / 多空行不影响 hash。
    a = compile_prompt([request_instructions_section("你是助手\n\n请用中文")])
    b = compile_prompt([request_instructions_section("你是助手\r\n\r\n\r\n请用中文   ")])
    assert a.content_hash == b.content_hash


def test_sources_from_instructions_default_volatile_only() -> None:
    sections = sections_from_instructions("你是助手")
    assert len(sections) == 1
    assert sections[0].kind == "request_instructions"
    assert sections[0].stability == "volatile"

    with_safety = sections_from_instructions("你是助手", include_platform_safety=True)
    assert [s.kind for s in with_safety] == ["platform_safety", "request_instructions"]
