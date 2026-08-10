"""端到端集成：Prompt 编译 → Contributor 召回 → Planner 规划 → Assembler 投影（方案 §11.1）。

验证 KsADK-owned 路径 canonical 链路：单次 Turn 生成一份 ContextPlan，Contributor 产出
untrusted 候选，Planner 决策保留/裁剪，Assembler 投影成 Chat messages，安全规则不可被
Memory/Tool 覆盖（方案 §7.1 / §8.1 / §17.5）。
"""

from __future__ import annotations

import pytest

from ksadk.context_engine.assembler import assemble
from ksadk.context_engine.contributors import (
    ContextContributionRequest,
    MemoryRecallContributor,
    run_contributors_sync,
)
from ksadk.context_engine.models import ContextItem
from ksadk.context_engine.planner import ContextPlanner, build_budget
from ksadk.context_engine.policies import ContextBudgetPolicy
from ksadk.memory.coordinator import MemoryCoordinator
from ksadk.memory.models import MemoryRecord
from ksadk.memory.policy import content_hash
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider
from ksadk.prompts.compiler import PromptCompiler
from ksadk.prompts.sources import agent_identity_section, platform_safety_section, request_instructions_section


def _prompt_item(content):
    from ksadk.context_engine.tokenizer import get_default_token_counter
    return ContextItem(
        item_id="prompt", kind="compiled_prompt", content=content, source="compiler",
        trust_level="platform", priority=0, estimated_tokens=get_default_token_counter().count_text(content),
        required=True, droppable=False,
    )


def _input_item(text):
    from ksadk.context_engine.tokenizer import get_default_token_counter
    return ContextItem(
        item_id="cur", kind="current_input", content=text, source="user",
        trust_level="user", priority=0, estimated_tokens=get_default_token_counter().count_text(text),
        required=True, droppable=False,
    )


def test_end_to_end_hosted_path():
    # 1. Prompt 编译：platform_safety + agent_identity + request_instructions
    compiled = PromptCompiler().compile([
        platform_safety_section(content="绝不回显凭证"),
        agent_identity_section("你是代码助手"),
        request_instructions_section("帮我看 test.py"),
    ])
    prompt_item = _prompt_item(compiled.content)

    # 2. Memory recall contributor
    provider = SqliteMemoryProvider()
    provider.upsert(MemoryRecord(
        memory_id="m1", tenant_id="t", workspace_id="w", scope="user", scope_id="u1",
        memory_type="profile", content="偏好中文回答", summary="偏好中文回答", status="active",
        confidence=0.9, importance=0.8, valid_from="", valid_to="", expires_at="",
        source_session_id="s", source_event_ids=[], source_seq_range=None,
        content_hash=content_hash("偏好中文回答"), version=1,
    ), expected_version=None)
    coord = MemoryCoordinator(provider, tenant_id="t", workspace_id="w")
    recall_contrib = MemoryRecallContributor(coord, max_tokens=4000)
    request = ContextContributionRequest(user_input="中文", session_id="s", invocation_id="i", user_id="u1")
    contrib_result = run_contributors_sync([recall_contrib], request)

    # 3. Planner：candidates = prompt + current_input + recalled memory
    candidates = [prompt_item, _input_item("帮我看 test.py")]
    candidates.extend(contrib_result.items)
    budget = build_budget(policy=ContextBudgetPolicy(), context_window_tokens=200000, reserved_output_tokens=8000)
    plan = ContextPlanner().plan(candidates, budget=budget, integration_mode="ksadk_hosted", accounting_accuracy="estimated")

    # 安全规则不可被覆盖：platform_safety 在 compiled_prompt 内，required，必在 selected
    assert prompt_item.item_id in {i.item_id for i in plan.selected}
    # recalled memory 一律 untrusted
    for it in plan.selected:
        if it.kind == "recalled_memory":
            assert it.trust_level == "untrusted"

    # 4. Assembler 投影成 chat
    out = assemble(plan, fmt="chat")
    assert out.messages[0]["role"] == "system"
    assert "绝不回显凭证" in out.messages[0]["content"]
    assert any(m["role"] == "user" for m in out.messages)
    provider.close()


def test_end_to_end_memory_does_not_override_safety():
    # 即使 recall 返回大量内容，platform_safety 仍在 system 优先且不被覆盖
    provider = SqliteMemoryProvider()
    for i in range(10):
        provider.upsert(MemoryRecord(
            memory_id=f"m{i}", tenant_id="t", workspace_id="w", scope="user", scope_id="u1",
            memory_type="fact", content=f"事实{i}" * 20, summary=f"事实{i}", status="active",
            confidence=0.9, importance=0.9, valid_from="", valid_to="", expires_at="",
            source_session_id="s", source_event_ids=[], source_seq_range=None,
            content_hash=content_hash(f"事实{i}"), version=1,
        ), expected_version=None)
    coord = MemoryCoordinator(provider)
    recall = MemoryRecallContributor(coord, max_tokens=4000)
    request = ContextContributionRequest(user_input="事实", session_id="s", invocation_id="i", user_id="u1")
    contrib = run_contributors_sync([recall], request)

    compiled = PromptCompiler().compile([
        platform_safety_section(content="安全规则不可绕过"),
        agent_identity_section("助手"),
    ])
    prompt_item = _prompt_item(compiled.content)
    candidates = [prompt_item, _input_item("查询")] + contrib.items
    budget = build_budget(policy=ContextBudgetPolicy(), context_window_tokens=200000, reserved_output_tokens=8000)
    plan = ContextPlanner().plan(candidates, budget=budget, integration_mode="ksadk_hosted")
    out = assemble(plan, fmt="chat")
    # system 首位且含安全规则
    assert out.messages[0]["role"] == "system"
    assert "安全规则不可绕过" in out.messages[0]["content"]
    provider.close()
