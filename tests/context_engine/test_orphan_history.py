"""长历史压力场景：user+assistant 原子组 + dropped 决策（方案 §8.1/§8.8）。

问题：大型 User 历史超 recent_history 预算被跳过，对应短 Assistant 继续进入 → 孤儿历史。
修复：_history_to_items 把 user+assistant 绑定同一 group_id；planner 分区超限记录 dropped。
"""

from __future__ import annotations

from ksadk.context_engine.hosted_pipeline import _history_to_items
from ksadk.context_engine.models import ContextBudget, ContextItem
from ksadk.context_engine.planner import ContextPlanner


def _budget(max_input, soft=None, hard=None, sections=None):
    soft = soft if soft is not None else max_input // 2
    hard = hard if hard is not None else int(max_input * 0.85)
    return ContextBudget(
        context_window_tokens=max_input + 8000,
        reserved_output_tokens=4000,
        reserved_reasoning_tokens=0,
        safety_buffer_tokens=8000,
        max_input_tokens=max_input,
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
        section_limits=sections or {},
    )


def test_history_items_pair_user_assistant_same_group():
    """user+assistant 绑定同一 group_id（不是各自独立 group）。"""
    history = [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "问题2"},
        {"role": "assistant", "content": "回答2"},
    ]
    items = _history_to_items(history)
    assert len(items) == 4
    # round 1: user + assistant 同组
    assert items[0].group_id == items[1].group_id == "round:1"
    # round 2: user + assistant 同组
    assert items[2].group_id == items[3].group_id == "round:2"
    # 不同轮不同组
    assert items[0].group_id != items[2].group_id


def test_no_orphan_assistant_when_large_user_dropped():
    """大型 User 被跳过时，同组 Assistant 也被跳过（不产生孤儿）。"""
    planner = ContextPlanner()
    # 构造：1 个 required prompt + 2 轮历史（user 大、assistant 小）
    prompt = ContextItem(
        item_id="prompt",
        kind="compiled_prompt",
        content="sys",
        source="compiler",
        trust_level="platform",
        priority=0,
        estimated_tokens=100,
        required=True,
        droppable=False,
    )
    big_user = ContextItem(
        item_id="hist:0",
        kind="history_round",
        content="x" * 5000,
        source="transcript",
        trust_level="developer",
        priority=0,
        estimated_tokens=5000,
        required=False,
        droppable=True,
        group_id="round:1",
        seq_start=0,
        metadata={"role": "user"},
    )
    short_assistant = ContextItem(
        item_id="hist:1",
        kind="history_round",
        content="ok",
        source="transcript",
        trust_level="developer",
        priority=0,
        estimated_tokens=2,
        required=False,
        droppable=True,
        group_id="round:1",
        seq_start=1,
        metadata={"role": "assistant"},
    )
    budget = _budget(max_input=200, soft=100, hard=170)
    plan = planner.plan(
        [prompt, big_user, short_assistant],
        budget=budget,
        integration_mode="ksadk_hosted",
    )
    selected_ids = {i.item_id for i in plan.selected}
    # prompt 必在
    assert "prompt" in selected_ids
    # user+assistant 同组：要么都在，要么都不在（不产生孤儿 assistant）
    user_in = "hist:0" in selected_ids
    assistant_in = "hist:1" in selected_ids
    assert user_in == assistant_in, f"孤儿历史：user_in={user_in}, assistant_in={assistant_in}"


def test_dropped_decision_recorded_for_skipped_group():
    """分区超限跳过的组记录 dropped 决策（方案 §8.8）。"""
    planner = ContextPlanner()
    prompt = ContextItem(
        item_id="prompt",
        kind="compiled_prompt",
        content="sys",
        source="compiler",
        trust_level="platform",
        priority=0,
        estimated_tokens=50,
        required=True,
        droppable=False,
    )
    big_user = ContextItem(
        item_id="hist:0",
        kind="history_round",
        content="x" * 5000,
        source="transcript",
        trust_level="developer",
        priority=0,
        estimated_tokens=5000,
        required=False,
        droppable=True,
        group_id="round:1",
        seq_start=0,
        metadata={"role": "user"},
    )
    short_assistant = ContextItem(
        item_id="hist:1",
        kind="history_round",
        content="ok",
        source="transcript",
        trust_level="developer",
        priority=0,
        estimated_tokens=2,
        required=False,
        droppable=True,
        group_id="round:1",
        seq_start=1,
        metadata={"role": "assistant"},
    )
    budget = _budget(max_input=200, soft=100, hard=170)
    plan = planner.plan(
        [prompt, big_user, short_assistant],
        budget=budget,
        integration_mode="ksadk_hosted",
    )
    # 被跳过的项有 dropped 决策
    dropped = [d for d in plan.decisions if d.action == "dropped"]
    if "hist:0" not in {i.item_id for i in plan.selected}:
        # 如果被跳过，应该有 dropped 决策
        dropped_ids = {d.item_id for d in dropped}
        assert "hist:0" in dropped_ids, f"被跳过的 user 应有 dropped 决策: {dropped_ids}"
        assert "hist:1" in dropped_ids, f"被跳过的 assistant 应有 dropped 决策: {dropped_ids}"
        # 决策有原因
        reasons = {d.reason for d in dropped if d.item_id in ("hist:0", "hist:1")}
        assert any("limit" in r or "exceeded" in r for r in reasons), (
            f"dropped 原因应含 limit/exceeded: {reasons}"
        )


def test_property_no_orphan_across_random_history():
    """property-based：随机历史不产生孤儿 assistant（user 在则 assistant 在，反之亦然）。"""
    import random

    planner = ContextPlanner()
    rng = random.Random(42)
    for _ in range(30):
        rounds = rng.randint(1, 8)
        candidates = [
            ContextItem(
                item_id="prompt",
                kind="compiled_prompt",
                content="sys",
                source="compiler",
                trust_level="platform",
                priority=0,
                estimated_tokens=50,
                required=True,
                droppable=False,
            )
        ]
        for r in range(rounds):
            user_tokens = rng.randint(10, 500)
            asst_tokens = rng.randint(5, 200)
            candidates.append(
                ContextItem(
                    item_id=f"hist:{r * 2}",
                    kind="history_round",
                    content="u" * user_tokens * 4,
                    source="transcript",
                    trust_level="developer",
                    priority=0,
                    estimated_tokens=user_tokens,
                    required=False,
                    droppable=True,
                    group_id=f"round:{r + 1}",
                    seq_start=r * 2,
                    metadata={"role": "user"},
                )
            )
            candidates.append(
                ContextItem(
                    item_id=f"hist:{r * 2 + 1}",
                    kind="history_round",
                    content="a" * asst_tokens * 4,
                    source="transcript",
                    trust_level="developer",
                    priority=0,
                    estimated_tokens=asst_tokens,
                    required=False,
                    droppable=True,
                    group_id=f"round:{r + 1}",
                    seq_start=r * 2 + 1,
                    metadata={"role": "assistant"},
                )
            )
        budget = _budget(max_input=rng.randint(100, 800))
        plan = planner.plan(candidates, budget=budget, integration_mode="ksadk_hosted")
        selected_ids = {i.item_id for i in plan.selected}
        # 每轮 user+assistant 要么都在要么都不在
        for r in range(rounds):
            u = f"hist:{r * 2}" in selected_ids
            a = f"hist:{r * 2 + 1}" in selected_ids
            assert u == a, f"round {r + 1} 孤儿：user_in={u}, assistant_in={a}"
