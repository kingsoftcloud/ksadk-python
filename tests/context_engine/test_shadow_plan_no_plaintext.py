"""PR B：shadow plan 不含 prompt_content 明文；接管态 integration_mode 覆盖但 capability_hash 稳定。"""

from __future__ import annotations

from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict
from ksadk.prompts.resolved import ResolvedPromptSources, compile_resolved_prompt_dict


def _real_compiled_dict(monkeypatch) -> dict:
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)
    return compile_resolved_prompt_dict(  # type: ignore[return-value]
        ResolvedPromptSources(
            agent_system="你是助手", agent_task="用中文", request_instructions="本轮"
        )
    )


def test_shadow_plan_strips_prompt_content(monkeypatch) -> None:
    compiled = _real_compiled_dict(monkeypatch)
    assert "prompt_content" in compiled  # 源 dict 含正文
    plan = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="langgraph",
    )
    # shadow plan / trace 不得含明文
    assert "prompt_content" not in plan
    # 但 hash/统计仍保留
    assert plan["prompt_stable_prefix_hash"].startswith("sha256:")
    assert plan["prompt_section_hashes"]
    # stable_prefix_hash 与真实 compiled 一致
    assert plan["prompt_stable_prefix_hash"] == compiled["prompt_stable_prefix_hash"]


def test_ksadk_hosted_takeover_overrides_integration_mode(monkeypatch) -> None:
    compiled = _real_compiled_dict(monkeypatch)
    # langgraph capability 默认 prompt_owner=ksadk（framework_assisted）
    plan = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="langgraph",
        prompt_integration_mode="ksadk_hosted",
    )
    assert plan["integration_mode"] == "ksadk_hosted"


def test_non_langgraph_runtime_does_not_show_ksadk_hosted(monkeypatch) -> None:
    compiled = _real_compiled_dict(monkeypatch)
    # 非 langgraph（codex/adk）即使标记 ksadk_hosted 也不覆盖（接管只限 LangGraph）
    plan = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="codex",
        prompt_integration_mode="ksadk_hosted",
    )
    assert plan["integration_mode"] != "ksadk_hosted"


def test_framework_mode_leaves_integration_mode_unchanged(monkeypatch) -> None:
    compiled = _real_compiled_dict(monkeypatch)
    plan_off = build_shadow_context_plan_dict(
        instructions="本轮", prompt_shadow=compiled, runtime_type="langgraph"
    )
    plan_framework = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="langgraph",
        prompt_integration_mode="",  # framework-owned
    )
    # 默认/framework：integration_mode == capability 原值，且二者相等（capability_hash 不随 mode 抖动）
    assert plan_off["integration_mode"] == plan_framework["integration_mode"]
    assert plan_off["capability_hash"] == plan_framework["capability_hash"]


def test_capability_hash_stable_across_takeover_toggle(monkeypatch) -> None:
    compiled = _real_compiled_dict(monkeypatch)
    plan_takeover = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="langgraph",
        prompt_integration_mode="ksadk_hosted",
    )
    plan_no_takeover = build_shadow_context_plan_dict(
        instructions="本轮",
        prompt_shadow=compiled,
        runtime_type="langgraph",
        prompt_integration_mode="",
    )
    # capability_hash 用原 caps，不随 per-request 接管状态抖动
    assert plan_takeover["capability_hash"] == plan_no_takeover["capability_hash"]
    # 但 integration_mode 不同（一个被覆盖）
    assert plan_takeover["integration_mode"] != plan_no_takeover["integration_mode"]
