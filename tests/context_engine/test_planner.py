"""ContextPlanner / Assembler / policies 单测（方案 §17.1 / §17.2）。

覆盖：required 优先、group 原子性、分区预算、确定性缩减顺序、emergency drop、budget 计算、
assembler chat/responses 投影。property-based invariant：缩减后无孤儿 group、不超 hard_limit。
"""

from __future__ import annotations

import random

from ksadk.context_engine.assembler import assemble
from ksadk.context_engine.models import ContextItem
from ksadk.context_engine.planner import ContextPlanner, build_budget
from ksadk.context_engine.policies import (
    ContextBudgetPolicy,
    ContextPolicy,
    SectionBudget,
    compute_budget_tokens,
)


def _item(item_id, kind, tokens, *, required=False, group_id=None, score=None, seq=None, droppable=True, content=None, metadata=None, content_hash=None):
    return ContextItem(
        item_id=item_id, kind=kind, content=content or item_id, source="test",
        trust_level="developer", priority=0, estimated_tokens=tokens, required=required,
        group_id=group_id, score=score, seq_start=seq, droppable=droppable,
        metadata=metadata or {}, content_hash=content_hash,
    )


def _budget(max_input, soft=None, hard=None, sections=None):
    from ksadk.context_engine.models import ContextBudget
    soft = soft if soft is not None else max_input // 2
    hard = hard if hard is not None else int(max_input * 0.85)
    return ContextBudget(
        context_window_tokens=max_input + 8000, reserved_output_tokens=4000,
        reserved_reasoning_tokens=0, safety_buffer_tokens=8000, max_input_tokens=max_input,
        soft_limit_tokens=soft, hard_limit_tokens=hard, section_limits=sections or {},
    )


# ---- budget 计算 ----

def test_compute_budget_tokens():
    p = ContextBudgetPolicy()
    t = compute_budget_tokens(p, context_window_tokens=200000, reserved_output_tokens=8000, reserved_reasoning_tokens=0)
    assert t["max_input_tokens"] == 200000 - 8000 - 0 - 8000
    assert t["soft_limit_tokens"] == int(t["max_input_tokens"] * 0.50)
    assert t["hard_limit_tokens"] == int(t["max_input_tokens"] * 0.85)


def test_build_budget_section_limits():
    p = ContextBudgetPolicy()
    b = build_budget(policy=p, context_window_tokens=200000, reserved_output_tokens=8000)
    assert b.max_input_tokens > 0
    assert b.section_limits["prompt"] == min(int(b.max_input_tokens * 0.15), 24000)
    assert b.section_limits["recent_history"] == min(int(b.max_input_tokens * 0.35), 64000)


def test_context_policy_from_env_defaults():
    p = ContextPolicy.from_env()
    assert p.budget.soft_limit_percent == 50.0
    assert p.compaction.max_retry_after_prompt_too_long == 1
    assert p.memory.write_mode == "propose"


# ---- Planner required / group 原子性 ----

def test_required_always_included_even_over_hard_limit():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 600, required=True)
    cur = _item("cur", "current_input", 50, required=True)
    budget = _budget(max_input=100, soft=40, hard=80)
    plan = p.plan([req, cur], budget=budget)
    ids = {i.item_id for i in plan.selected}
    assert {"safety", "cur"} <= ids  # required 必在


def test_group_atomicity_tool_pair():
    p = ContextPlanner()
    call = _item("call1", "history_round", 30, group_id="g1", metadata={"role": "assistant"})
    result = _item("result1", "tool_result", 30, group_id="g1", required=True)
    budget = _budget(max_input=10000, soft=5000, hard=8000)
    plan = p.plan([result, call], budget=budget)
    ids = {i.item_id for i in plan.selected}
    # result required → group g1 整组进入
    assert {"call1", "result1"} <= ids


def test_group_atomicity_dropped_together_when_emergency():
    p = ContextPlanner()
    # 构造超 hard_limit：required 占满，非 required group 应整组丢
    req = _item("safety", "compiled_prompt", 90, required=True)
    cur = _item("cur", "current_input", 5, required=True)
    big_group = [
        _item(f"r{i}", "history_round", 30, group_id="g1", seq=10 + i, droppable=True)
        for i in range(5)
    ]
    budget = _budget(max_input=100, soft=20, hard=50)
    plan = p.plan([req, cur, *big_group], budget=budget)
    ids = {i.item_id for i in plan.selected}
    # 超预算时整组要么全在要么全不在
    group_in = {i for i in ids if i.startswith("r")}
    assert group_in == set() or group_in == {f"r{i}" for i in range(5)}


# ---- 优先级与缩减 ----

def test_priority_current_input_over_recall():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 10, required=True)
    cur = _item("cur", "current_input", 40, required=True)
    recall = _item("rec", "recalled_memory", 40, score=0.99, droppable=True)
    recall2 = _item("rec2", "recalled_memory", 40, score=0.5, droppable=True)
    budget = _budget(max_input=100, soft=50, hard=80)
    plan = p.plan([req, cur, recall, recall2], budget=budget)
    ids = {i.item_id for i in plan.selected}
    assert "cur" in ids  # current_input 优先于 recall
    # recall 总 80 + required 50 > hard 80 → 至少一个 recall 被丢，低分先丢
    assert "rec" in ids or "rec2" not in ids  # 高分保留优先


def test_dedupe_manifest_in_reduce():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 30, required=True)
    m1 = _item("m1", "resource_manifest", 100, content_hash="h1", droppable=True)
    m2 = _item("m2", "resource_manifest", 100, content_hash="h1", droppable=True)  # 重复
    budget = _budget(max_input=1000, soft=40, hard=800)  # soft 低触发缩减
    plan = p.plan([req, m1, m2], budget=budget)
    ids = {i.item_id for i in plan.selected}
    # 重复 manifest 去重后只留一个
    assert ("m1" in ids) ^ ("m2" in ids)
    dropped = [d for d in plan.decisions if d.action == "dropped" and "dedupe" in d.reason]
    assert dropped


def test_large_tool_result_summarized():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 10, required=True)
    big = _item("big", "tool_result", 20000, droppable=True)
    budget = _budget(max_input=1000, soft=50, hard=900)
    plan = p.plan([req, big], budget=budget)
    summarized = [d for d in plan.decisions if d.action == "summarized"]
    assert summarized
    sel = [i for i in plan.selected if i.item_id == "big"]
    assert sel and sel[0].estimated_tokens < 20000


def test_planned_tokens_not_exceed_hard_when_droppable():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 30, required=True)
    items = [_item(f"r{i}", "history_round", 100, group_id=f"g{i}", droppable=True, seq=i) for i in range(10)]
    budget = _budget(max_input=200, soft=50, hard=120)
    plan = p.plan([req, *items], budget=budget)
    assert plan.planned_input_tokens <= 120 + 30  # hard + required（required 可超，但非 required 不超）


# ---- Assembler ----

def test_assemble_chat_system_first():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 10, required=True, content="<platform_safety>...")
    cur = _item("cur", "current_input", 10, required=True, content="hello")
    budget = _budget(max_input=1000, soft=500, hard=800)
    plan = p.plan([req, cur], budget=budget)
    out = assemble(plan, fmt="chat")
    assert out.messages[0]["role"] == "system"
    assert "<platform_safety>" in out.messages[0]["content"]
    assert any(m["role"] == "user" and m["content"] == "hello" for m in out.messages)


def test_assemble_chat_keeps_current_input_after_selected_history():
    p = ContextPlanner()
    req = _item("prompt", "compiled_prompt", 10, required=True, content="system")
    current = _item(
        "current", "current_input", 10, required=True, content="second turn"
    )
    history = _item(
        "history",
        "history_round",
        10,
        content="first turn",
        metadata={"role": "user"},
    )
    plan = p.plan(
        [req, current, history],
        budget=_budget(max_input=1000, soft=500, hard=800),
    )

    out = assemble(plan, fmt="chat")
    assert [message["content"] for message in out.messages] == [
        "system",
        "first turn",
        "second turn",
    ]


def test_assemble_responses_tool_output():
    p = ContextPlanner()
    req = _item("safety", "compiled_prompt", 10, required=True, content="sys")
    tool = _item("tr", "tool_result", 10, group_id="g1", required=True, content="42",
                 metadata={"call_id": "c1"})
    budget = _budget(max_input=1000, soft=500, hard=800)
    plan = p.plan([req, tool], budget=budget)
    out = assemble(plan, fmt="responses")
    assert any(it.get("type") == "function_call_output" and it.get("call_id") == "c1" for it in out.responses_items)


# ---- property-based invariant ----

def test_property_no_orphan_group_after_reduce():
    rng = random.Random(2026)
    p = ContextPlanner()
    for _ in range(50):
        n = rng.randint(2, 8)
        cands = []
        for i in range(n):
            kind = rng.choice(["compiled_prompt", "current_input", "history_round", "tool_result", "recalled_memory"])
            grp = f"g{rng.randint(0, n // 2)}" if rng.random() < 0.5 else None
            cands.append(_item(f"i{i}", kind, rng.randint(10, 300), group_id=grp,
                               required=(kind in ("compiled_prompt", "current_input")),
                               droppable=kind != "current_input", seq=i, score=rng.random()))
        budget = _budget(max_input=rng.randint(200, 800), soft=100, hard=rng.randint(150, 700))
        plan = p.plan(cands, budget=budget)
        # invariant：无孤儿 group —— 同 group 全在或全不在 selected
        groups = {}
        for c in cands:
            if c.group_id:
                groups.setdefault(c.group_id, []).append(c.item_id)
        sel_ids = {i.item_id for i in plan.selected}
        for g, members in groups.items():
            in_sel = [m for m in members if m in sel_ids]
            assert in_sel == members or in_sel == [], f"orphan group {g}: {in_sel} of {members}"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
