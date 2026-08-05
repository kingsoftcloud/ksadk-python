from __future__ import annotations

from types import SimpleNamespace

from ksadk.context_engine.capabilities import codex_context_capabilities, langgraph_context_capabilities
from ksadk.context_engine.shadow_plan import (
    build_shadow_context_plan_dict,
    minimal_shadow_context_plan_dict,
)
from ksadk.conversations.model_context import estimate_text_tokens


def test_build_shadow_plan_accumulates_tokens_by_kind() -> None:
    instructions = "你是助手，遵循安全规则"
    history = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
    ]
    user_input = "继续"
    plan = build_shadow_context_plan_dict(
        instructions=instructions,
        history=history,
        user_input=user_input,
    )
    assert plan["policy_version"] == "v1"
    assert plan["tokenizer"] == "heuristic_cjk_ascii"
    assert plan["plan_id"].startswith("ctxplan_")

    expected_prompt = estimate_text_tokens(instructions)
    expected_history = estimate_text_tokens("user") + estimate_text_tokens("第一轮问题") + estimate_text_tokens(
        "assistant"
    ) + estimate_text_tokens("第一轮回答")
    expected_input = estimate_text_tokens(user_input)

    assert plan["tokens_by_kind"]["compiled_prompt"] == expected_prompt
    assert plan["tokens_by_kind"]["history_round"] == expected_history
    assert plan["tokens_by_kind"]["current_input"] == expected_input
    assert plan["tokens_by_kind"]["recalled_memory"] == 0
    assert plan["planned_input_tokens"] == expected_prompt + expected_history + expected_input


def test_build_shadow_plan_picks_up_memory_and_kb_ambient() -> None:
    plan = build_shadow_context_plan_dict(
        user_input="x",
        request_metadata={
            "memory_context": {"formatted_text": "记住用 Python 3.12"},
            "kb_context": {"formatted_text": "知识库片段"},
        },
    )
    assert plan["tokens_by_kind"]["recalled_memory"] == estimate_text_tokens("记住用 Python 3.12")
    assert plan["tokens_by_kind"]["attachment_context"] == estimate_text_tokens("知识库片段")


def test_build_shadow_plan_runner_none_is_opaque() -> None:
    plan = build_shadow_context_plan_dict(user_input="x", runner=None)
    assert plan["integration_mode"] == "framework_assisted"
    assert plan["accounting_accuracy"] == "opaque"


def test_build_shadow_plan_runner_accuracy_aligns_with_capability() -> None:
    # LangGraph → estimated
    langgraph_runner = SimpleNamespace(
        detection_result=SimpleNamespace(type=SimpleNamespace(value="langgraph"))
    )
    plan = build_shadow_context_plan_dict(user_input="x", runner=langgraph_runner)
    assert plan["integration_mode"] == langgraph_context_capabilities().integration_mode
    assert plan["accounting_accuracy"] == "estimated"
    assert plan["compaction_owner"] == "ksadk"

    # Codex → runtime_reported + native owners
    codex_runner = SimpleNamespace(
        detection_result=SimpleNamespace(type=SimpleNamespace(value="codex"))
    )
    plan = build_shadow_context_plan_dict(user_input="x", runner=codex_runner)
    assert plan["integration_mode"] == codex_context_capabilities().integration_mode
    assert plan["accounting_accuracy"] == "runtime_reported"
    assert plan["history_owner"] == "native"


def test_minimal_shadow_plan_zero_tokens_and_carries_ownership() -> None:
    plan = minimal_shadow_context_plan_dict(runner=None)
    assert plan["planned_input_tokens"] == 0
    assert all(v == 0 for v in plan["tokens_by_kind"].values())
    assert plan["accounting_accuracy"] == "opaque"

    codex_runner = SimpleNamespace(
        detection_result=SimpleNamespace(type=SimpleNamespace(value="codex"))
    )
    minimal = minimal_shadow_context_plan_dict(runner=codex_runner)
    assert minimal["integration_mode"] == "native_runtime"
    assert minimal["memory_owner"] == "native"


def test_build_shadow_plan_returns_plain_dict_not_context_plan_object() -> None:
    plan = build_shadow_context_plan_dict(user_input="x")
    assert isinstance(plan, dict)
    # 不携带 ContextItem/ContextDecision（selected/decisions 留后续 PR），避免误用。
    assert "selected" not in plan
    assert "decisions" not in plan


def test_shadow_plan_carries_compiled_prompt_hashes() -> None:
    plan = build_shadow_context_plan_dict(instructions="你是助手", user_input="hi")
    # 编译确定性：相同 instructions → 相同 prompt_content_hash。
    again = build_shadow_context_plan_dict(instructions="你是助手", user_input="hi")
    assert plan["prompt_content_hash"]
    assert plan["prompt_content_hash"] == again["prompt_content_hash"]
    # shadow plan 的 stable_prefix_hash 与编译结果一致；仅 volatile section 时为空。
    assert plan["stable_prefix_hash"] == plan["prompt_stable_prefix_hash"]
    assert "request_instructions" in plan["prompt_section_hashes"]
    assert plan["prompt_tokens_by_section"]["request_instructions"] > 0


def test_shadow_plan_empty_instructions_has_empty_prompt_hashes() -> None:
    plan = build_shadow_context_plan_dict(instructions="", user_input="x")
    assert plan["prompt_content_hash"] == ""
    assert plan["prompt_stable_prefix_hash"] == ""
    assert plan["prompt_section_hashes"] == {}
