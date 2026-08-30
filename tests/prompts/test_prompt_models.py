from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace

import pytest

from ksadk.prompts import (
    PROMPT_COMPILER_VERSION,
    CompiledPrompt,
    PromptProjectionResult,
    PromptSection,
)


def _section(kind: str = "agent_identity", priority: int = 20) -> PromptSection:
    return PromptSection(
        section_id=f"sec_{kind}",
        kind=kind,  # type: ignore[arg-type]
        content="你是示例助手",
        source="agent_bundle",
        priority=priority,
        trust_level="developer",
    )


def test_prompt_section_is_frozen_with_default_metadata() -> None:
    section = _section()
    assert section.stability == "stable"
    assert section.merge_policy == "append"
    assert section.overridable is False
    assert section.metadata == {}
    with pytest.raises(FrozenInstanceError):
        section.content = "改不了"  # type: ignore[misc]


def test_compiled_prompt_is_frozen_and_carries_compiler_version() -> None:
    compiled = CompiledPrompt(
        sections=(_section("platform_safety", 10), _section("agent_identity", 20)),
        content="<platform_safety>\n</platform_safety>\n\n<agent_identity>\n你是示例助手\n</agent_identity>",
        content_hash="sha256:abc",
        estimated_tokens=12,
        stable_prefix_hash="sha256:prefix",
        section_hashes={"sec_platform_safety": "sha256:p", "sec_agent_identity": "sha256:a"},
        tokens_by_section={"sec_platform_safety": 4, "sec_agent_identity": 8},
    )
    assert compiled.compiler_version == PROMPT_COMPILER_VERSION
    # 字段名稳定（公开合同从第一批开始版本化）
    assert {f.name for f in fields(CompiledPrompt)} >= {
        "sections",
        "content",
        "content_hash",
        "estimated_tokens",
        "stable_prefix_hash",
        "section_hashes",
        "tokens_by_section",
        "compiler_version",
    }
    with pytest.raises(FrozenInstanceError):
        compiled.content_hash = "x"  # type: ignore[misc]
    # replace 仍可生成新实例（frozen dataclass 不可变但可派生）
    derived = replace(compiled, estimated_tokens=20)
    assert derived.estimated_tokens == 20 and compiled.estimated_tokens == 12


def test_prompt_projection_result_defaults() -> None:
    result = PromptProjectionResult(
        projection_id="proj_1",
        runner_type="langgraph",
        integration_mode="framework_assisted",
        projection_version="v1",
        section_hashes=("sha256:a",),
        projected_roles=("system",),
        accounting_accuracy="estimated",
        estimated_tokens=None,
    )
    assert result.warnings == ()
