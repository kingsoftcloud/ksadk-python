"""ContextPlanner —— 预算、required/group 原子性与确定性缩减（方案 §8.4 / §8.5）。

Planner 是 Context Engine 的决策核心：输入候选项 ``ContextItem`` 与 ``ContextBudget``，
输出 ``ContextPlan``（selected + decisions）。强制优先级遵循方案 §8.4 第 11 条；group 原子性
保证 tool call/result、approval request/response、Responses call/output 不被拆散
（方案 §8.1 ``group_id``）。

本模块纯计算、无副作用、可复用；不接触 Session Store、不调用模型。replan 由调用方在
compaction/PTL 后通过新 ``plan_id`` 重新调用（方案 §11.1 / ADR-016）。
"""

from __future__ import annotations

import uuid
from typing import Iterable

from ksadk.context_engine.models import (
    CONTEXT_POLICY_VERSION,
    ContextBudget,
    ContextDecision,
    ContextItem,
    ContextKind,
    ContextPlan,
)
from ksadk.context_engine.policies import ContextBudgetPolicy, SectionBudget

# 强制优先级（方案 §8.4）：数字越小越优先保留。
# platform_safety(1) → current_input(2) → pending approval/tool(3) → receipt/framework ref(4)
# → agent identity/policy(5) → checkpoint summary(6) → working state(7) → recent rounds(8)
# → core memory(9) → recall memory(10) → optional skill/旧 tool/附件(11)
_PRIORITY_RANK: dict[ContextKind, int] = {
    "compiled_prompt": 1,  # 含 platform_safety/agent_identity/agent_policy
    "current_input": 2,
    "tool_result": 3,  # pending tool 状态随 round 保留
    "working_state": 7,
    "checkpoint_summary": 6,
    "core_memory": 9,
    "recalled_memory": 10,
    "history_round": 8,
    "skill_content": 11,
    "attachment_context": 11,
    "resource_manifest": 5,
}

# 分区 key 映射（方案 §8.3 分区预算表 → ContextKind）。
_KIND_TO_SECTION: dict[ContextKind, str] = {
    "compiled_prompt": "prompt",
    "resource_manifest": "resource_manifest",
    "core_memory": "core_memory",
    "recalled_memory": "recalled_memory",
    "checkpoint_summary": "checkpoint_summary",
    "working_state": "working_state",
    "history_round": "recent_history",
    "tool_result": "tool_and_attachment",
    "attachment_context": "tool_and_attachment",
    "skill_content": "tool_and_attachment",
    "current_input": "recent_history",
}


def _tokens(items: Iterable[ContextItem]) -> int:
    return sum(i.estimated_tokens for i in items)


def _group_groups(items: list[ContextItem]) -> dict[str, list[ContextItem]]:
    """按 ``group_id`` 聚合，无 group_id 的各自成组。"""
    groups: dict[str, list[ContextItem]] = {}
    for item in items:
        key = item.group_id or item.item_id
        groups.setdefault(key, []).append(item)
    return groups


def _section_for(kind: ContextKind) -> str:
    return _KIND_TO_SECTION.get(kind, "tool_and_attachment")


class ContextPlanner:
    """确定性 Context 规划器（方案 §8.4 / §8.5）。

    ``plan()`` 对相同输入产生相同输出（确定性排序 + 确定性缩减）。无状态、可复用。
    """

    def __init__(self, *, policy: ContextBudgetPolicy | None = None) -> None:
        self._policy = policy or ContextBudgetPolicy()

    def plan(
        self,
        candidates: list[ContextItem],
        *,
        budget: ContextBudget,
        integration_mode: str = "ksadk_hosted",
        accounting_accuracy: str = "estimated",
        tokenizer: str = "heuristic_cjk_ascii",
        stable_prefix_hash: str = "",
    ) -> ContextPlan:
        plan_id = f"ctxplan_{uuid.uuid4().hex[:16]}"
        decisions: list[ContextDecision] = []

        # 1. required 先锁定（方案 §8.4）。required 超 hard_limit → 配置错误，仍返回 plan 但标 dropped。  # noqa: E501
        required = self._select_required(candidates, budget.hard_limit_tokens, decisions)
        selected = list(required)

        # 2. 非 required 按 §8.4 优先级排序后增量加入，遵守 group 原子性 + 分区预算。
        non_required = [
            c
            for c in candidates
            if not c.required and c.item_id not in {i.item_id for i in selected}
        ]
        non_required.sort(key=self._sort_key)
        selected = self._add_within_budget(
            selected, non_required, budget, decisions, strict_section_limits=True
        )

        # 3. soft limit → 确定性缩减（零 LLM 成本，方案 §8.5）
        if _tokens(selected) > budget.soft_limit_tokens:
            selected = self._deterministic_reduce(selected, budget.soft_limit_tokens, decisions)

        # 4. hard limit → 紧急缩减（仍零 LLM；semantic compaction 由调用方在后续触发，方案 §8.4 末）
        if _tokens(selected) > budget.hard_limit_tokens:
            selected = self._emergency_reduce(selected, budget.hard_limit_tokens, decisions)

        tokens_by_kind = self._tokens_by_kind(selected)
        return ContextPlan(
            plan_id=plan_id,
            policy_version=CONTEXT_POLICY_VERSION,
            tokenizer=tokenizer,
            integration_mode=integration_mode,  # type: ignore[arg-type]
            accounting_accuracy=accounting_accuracy,  # type: ignore[arg-type]
            budget=budget,
            selected=selected,
            decisions=decisions,
            tokens_by_kind=tokens_by_kind,
            planned_input_tokens=_tokens(selected),
            projected_input_tokens=None,
            runtime_reported_input_tokens=None,
            stable_prefix_hash=stable_prefix_hash,
            projection_id=None,
            contributor_status={},
        )

    # ---- 步骤实现 ----

    def _select_required(
        self,
        candidates: list[ContextItem],
        hard_limit: int,
        decisions: list[ContextDecision],
    ) -> list[ContextItem]:
        """required 先锁定，并按 group 原子性拉入同组非 required 成员（方案 §8.1）。

        group 原子性是跨全体 candidates 的：只要 group 中有 required，整组进入；非 required
        成员不进非 required 增量阶段，避免孤儿 group。
        """
        required = [c for c in candidates if c.required]
        required.sort(key=self._sort_key)
        all_groups = _group_groups(candidates)
        selected: list[ContextItem] = []
        seen: set[str] = set()
        # required 本身总是进入（即使超 hard_limit，标 included；调用方据 hard_limit 判配置错误）。
        for item in required:
            selected.append(item)
            seen.add(item.item_id)
            decisions.append(
                ContextDecision(
                    item_id=item.item_id,
                    action="included",
                    reason="required",
                    tokens_before=item.estimated_tokens,
                    tokens_after=item.estimated_tokens,
                )
            )
        # 拉入 required 所在 group 的非 required 成员（原子保留，方案 §8.1）。
        for item in required:
            if not item.group_id:
                continue
            for mate in all_groups.get(item.group_id, []):
                if mate.item_id in seen:
                    continue
                selected.append(mate)
                seen.add(mate.item_id)
                decisions.append(
                    ContextDecision(
                        item_id=mate.item_id,
                        action="included",
                        reason="required_group_atomic",
                        tokens_before=mate.estimated_tokens,
                        tokens_after=mate.estimated_tokens,
                    )
                )
        return selected

    def _sort_key(self, item: ContextItem) -> tuple[int, int, str]:
        rank = _PRIORITY_RANK.get(item.kind, 99)
        # 同优先级：required 先、score 高先、seq 小先
        score = item.score if item.score is not None else 0.0
        seq = item.seq_start if item.seq_start is not None else 0
        return (rank, -int(score * 1000), seq, item.item_id)

    def _add_within_budget(
        self,
        selected: list[ContextItem],
        candidates: list[ContextItem],
        budget: ContextBudget,
        decisions: list[ContextDecision],
        *,
        strict_section_limits: bool,
    ) -> list[ContextItem]:
        chosen_ids = {i.item_id for i in selected}
        section_used: dict[str, int] = self._section_used(selected)
        groups = _group_groups(candidates)
        # 按 group 的最小优先级排序，保证整组按优先级进入
        group_order = sorted(
            groups.items(),
            key=lambda kv: min(self._sort_key(m) for m in kv[1]),
        )
        for _gkey, members in group_order:
            group_tokens = _tokens(members)
            section = _section_for(members[0].kind)
            limit = budget.section_limits.get(section)
            # group 原子性：整组进或不进（除非单组就超 hard_limit，则尝试截断 truncatable）
            if _tokens(selected) + group_tokens > budget.hard_limit_tokens:
                # 尝试 truncatable 单项截断
                added = self._try_truncate_into(selected, members, budget, decisions)
                selected.extend(added)
                if not added:
                    # 整组因 hard_limit 被跳过 → 记录 dropped 决策（方案 §8.8）
                    for m in members:
                        if m.item_id not in chosen_ids:
                            self._drop(
                                decisions,
                                m,
                                "hard_limit_exceeded",
                            )
                continue
            if (
                strict_section_limits
                and limit is not None
                and section_used.get(section, 0) + group_tokens > limit
            ):
                # 分区预算超限：整组跳过 → 记录 dropped 决策（方案 §8.8）
                for m in members:
                    if m.item_id not in chosen_ids:
                        self._drop(
                            decisions,
                            m,
                            f"section_limit:{section}",
                        )
                continue
            # 全组成员未选 → 整组进入
            new_members = [m for m in members if m.item_id not in chosen_ids]
            if not new_members:
                continue
            selected.extend(new_members)
            section_used[section] = section_used.get(section, 0) + _tokens(new_members)
            for m in new_members:
                decisions.append(
                    ContextDecision(
                        item_id=m.item_id,
                        action="included",
                        reason=f"section:{section}",
                        tokens_before=m.estimated_tokens,
                        tokens_after=m.estimated_tokens,
                    )
                )
        return selected

    def _try_truncate_into(
        self,
        selected: list[ContextItem],
        members: list[ContextItem],
        budget: ContextBudget,
        decisions: list[ContextDecision],
    ) -> list[ContextItem]:
        """整组超 hard_limit 时原子抢救：固定成员保留，其余截断或摘要（方案 §8.1/§8.6）。

        即使 Tool Result 可降载，Tool Call/Result 仍是一个协议组，不能只留下 Result。
        因此先为不可缩减成员预留预算，再处理可缩减成员；任一成员无法进入时整组放弃。
        """
        from dataclasses import replace

        def _reducible(item: ContextItem) -> bool:
            return bool(
                item.truncatable
                or (item.kind == "tool_result" and item.droppable and not item.required)
            )

        remaining_total = budget.hard_limit_tokens - _tokens(selected)
        fixed = [item for item in members if not _reducible(item)]
        fixed_tokens = _tokens(fixed)
        reducible = [item for item in members if _reducible(item)]
        if fixed_tokens > remaining_total or (reducible and fixed_tokens >= remaining_total):
            return []

        added: list[ContextItem] = list(fixed)
        pending_decisions: list[ContextDecision] = [
            ContextDecision(
                item_id=item.item_id,
                action="included",
                reason="group_atomic_fixed",
                tokens_before=item.estimated_tokens,
                tokens_after=item.estimated_tokens,
            )
            for item in fixed
        ]
        for index, m in enumerate(reducible):
            remaining = budget.hard_limit_tokens - _tokens(selected) - _tokens(added)
            # 至少给后续每个可缩减成员留 1 token，保证整组原子进入。
            available = remaining - (len(reducible) - index - 1)
            if available <= 0:
                return []

            # 大 tool_result → artifact summary（方案 §8.6：保留 error tail + 引用）
            if (
                m.kind == "tool_result"
                and m.droppable
                and not m.required
                and m.estimated_tokens > available
            ):
                after = max(1, min(available, m.estimated_tokens // 8 + 200))
                added.append(
                    replace(
                        m,
                        estimated_tokens=after,
                        metadata={**m.metadata, "replaced_with_artifact_summary": True},
                    )
                )
                pending_decisions.append(
                    ContextDecision(
                        item_id=m.item_id,
                        action="summarized",
                        reason="large_tool_result_to_artifact",
                        tokens_before=m.estimated_tokens,
                        tokens_after=after,
                    )
                )
                continue
            if not m.truncatable or m.estimated_tokens == 0:
                # 可缩减集合里的非 truncatable 项只能是尚未超过 available 的 Tool Result。
                if m.estimated_tokens > available:
                    return []
                added.append(m)
                pending_decisions.append(
                    ContextDecision(
                        item_id=m.item_id,
                        action="included",
                        reason="group_atomic_fit",
                        tokens_before=m.estimated_tokens,
                        tokens_after=m.estimated_tokens,
                    )
                )
                continue
            # 截断到剩余预算（启发式按 token 比例截字符；实际截断由 assembler 处理）
            ratio = available / max(m.estimated_tokens, 1)
            after = max(0, int(m.estimated_tokens * ratio))
            if after == 0:
                return []
            added.append(
                replace(
                    m, estimated_tokens=after, metadata={**m.metadata, "truncated_to_tokens": after}
                )
            )
            pending_decisions.append(
                ContextDecision(
                    item_id=m.item_id,
                    action="truncated",
                    reason="hard_limit_truncate",
                    tokens_before=m.estimated_tokens,
                    tokens_after=after,
                )
            )
        if len(added) != len(members):
            return []
        decisions.extend(pending_decisions)
        return added

    def _deterministic_reduce(
        self, selected: list[ContextItem], soft_limit: int, decisions: list[ContextDecision]
    ) -> list[ContextItem]:
        """零 LLM 成本的确定性缩减（方案 §8.5 1-6 步）。

        顺序：删除重复 manifest → 大 tool result 转 artifact reference → 移除被覆盖旧
        tool call/result → 去二进制/重复日志 → 旧冷轮次 microcompact（此处只 drop 冷轮）→
        降低 recall top_k。required 与 current_input 不动。
        """
        kept = list(selected)
        {i.item_id for i in kept}

        # 1. 重复 resource_manifest（同 content_hash 去重）
        seen_hashes: set[str] = set()
        new_kept: list[ContextItem] = []
        for item in kept:
            if item.kind == "resource_manifest" and item.content_hash:
                if item.content_hash in seen_hashes:
                    self._drop(decisions, item, "dedupe_manifest")
                    continue
                seen_hashes.add(item.content_hash)
            new_kept.append(item)
        kept = new_kept
        if _tokens(kept) <= soft_limit:
            return kept

        # 2. 大 tool_result 转摘要（droppable 且非 required）
        kept = self._reduce_large_tool_results(kept, decisions)
        if _tokens(kept) <= soft_limit:
            return kept

        # 3. 移除被同参数覆盖的旧 tool call/result（metadata.overwritten_by）
        kept = self._drop_overwritten_tools(kept, decisions)
        if _tokens(kept) <= soft_limit:
            return kept

        # 5. 旧冷轮次 drop（非 required、非 current_input、seq 最早的 history_round）
        kept = self._drop_cold_rounds(kept, decisions, soft_limit)
        if _tokens(kept) <= soft_limit:
            return kept

        # 6. 降低 recall memory top_k（按 score 最低先丢）
        kept = self._reduce_recall(kept, decisions, soft_limit)
        return kept

    def _emergency_reduce(
        self, selected: list[ContextItem], hard_limit: int, decisions: list[ContextDecision]
    ) -> list[ContextItem]:
        """紧急缩减：在确定性缩减基础上按优先级逆序丢非 required 可丢项（方案 §8.4）。

        group 原子性：只有当一个 group 的**全体**成员都是可丢且非 required 时才整组丢；否则该
        group 保持完整（避免孤儿 group）。required/current_input 永不丢。
        """
        kept = list(selected)
        # 计算每个 group 是否可整组丢（全体 droppable 且非 required 且非 current_input）。
        all_groups = _group_groups(kept)
        droppable_groups: set[str] = set()
        for gkey, members in all_groups.items():
            if all(m.droppable and not m.required and m.kind != "current_input" for m in members):
                droppable_groups.add(gkey)
        # 候选丢弃单元：可整组丢的 group + 无 group 的可丢单项
        drop_candidates: list[tuple[int, list[ContextItem]]] = []
        for gkey, members in all_groups.items():
            if gkey in droppable_groups:
                drop_candidates.append(
                    (min(_PRIORITY_RANK.get(m.kind, 99) for m in members), members)
                )
            else:
                # 无 group 的可丢单项
                for m in members:
                    if (
                        not m.group_id
                        and m.droppable
                        and not m.required
                        and m.kind != "current_input"
                    ):
                        drop_candidates.append((_PRIORITY_RANK.get(m.kind, 99), [m]))
        # 按优先级逆序丢（rank 大先丢）
        drop_candidates.sort(key=lambda x: (-x[0],))
        for _rank, members in drop_candidates:
            if _tokens(kept) <= hard_limit:
                break
            for m in members:
                if m in kept:
                    kept.remove(m)
                    self._drop(decisions, m, "emergency_drop")
        return kept

    # ---- 缩减子步骤 ----

    def _reduce_large_tool_results(
        self, kept: list[ContextItem], decisions: list[ContextDecision]
    ) -> list[ContextItem]:
        new_kept: list[ContextItem] = []
        for item in kept:
            if (
                item.kind == "tool_result"
                and item.droppable
                and not item.required
                and item.estimated_tokens
                > self._policy.sections.get(
                    "tool_and_attachment", SectionBudget(10, 16000)
                ).max_tokens
            ):
                # 转摘要：保留 error tail + artifact reference（这里以估算 1/8 表达，assembler 真正截断）  # noqa: E501
                from dataclasses import replace

                after = max(item.estimated_tokens // 8, 200)
                new_kept.append(
                    replace(
                        item,
                        estimated_tokens=after,
                        metadata={**item.metadata, "replaced_with_artifact_summary": True},
                    )
                )
                decisions.append(
                    ContextDecision(
                        item_id=item.item_id,
                        action="summarized",
                        reason="large_tool_result_to_artifact",
                        tokens_before=item.estimated_tokens,
                        tokens_after=after,
                    )
                )
            else:
                new_kept.append(item)
        return new_kept

    def _drop_overwritten_tools(
        self, kept: list[ContextItem], decisions: list[ContextDecision]
    ) -> list[ContextItem]:
        overwritten = {
            item.metadata.get("overwritten_by")
            for item in kept
            if item.kind == "tool_result" and item.metadata.get("overwritten_by")
        }
        if not overwritten:
            return kept
        all_groups = _group_groups(kept)
        droppable_groups: set[str] = set()
        for gkey, members in all_groups.items():
            if all(m.droppable and not m.required for m in members):
                droppable_groups.add(gkey)
        # 标记要丢弃的 group 与单项
        drop_groups: set[str] = set()
        drop_items: set[str] = set()
        for item in kept:
            if not (
                item.kind == "tool_result"
                and item.content_hash in overwritten
                and not item.required
            ):
                continue
            if item.group_id:
                if item.group_id in droppable_groups:
                    drop_groups.add(item.group_id)
            else:
                drop_items.add(item.item_id)
        if not drop_groups and not drop_items:
            return kept
        new_kept: list[ContextItem] = []
        for item in kept:
            if item.item_id in drop_items or (item.group_id and item.group_id in drop_groups):
                self._drop(decisions, item, "overwritten_tool_result")
                continue
            new_kept.append(item)
        return new_kept

    def _drop_cold_rounds(
        self, kept: list[ContextItem], decisions: list[ContextDecision], soft_limit: int
    ) -> list[ContextItem]:
        all_groups = _group_groups(kept)
        # 只丢全体可丢且非 required 的 group（避免孤儿）。
        droppable_groups: set[str] = set()
        for gkey, members in all_groups.items():
            if all(m.droppable and not m.required for m in members):
                droppable_groups.add(gkey)
        rounds = [i for i in kept if i.kind == "history_round" and i.droppable and not i.required]
        rounds.sort(key=lambda i: (i.seq_start if i.seq_start is not None else 0,))
        for item in rounds:
            if _tokens(kept) <= soft_limit:
                break
            if item.group_id and item.group_id not in droppable_groups:
                continue  # 组内有 required/非可丢成员，保持完整
            if item.group_id:
                group = [i for i in kept if i.group_id == item.group_id]
                for g in group:
                    if g in kept:
                        kept.remove(g)
                        self._drop(decisions, g, "cold_round_drop")
            else:
                if item in kept:
                    kept.remove(item)
                    self._drop(decisions, item, "cold_round_drop")
        return kept

    def _reduce_recall(
        self, kept: list[ContextItem], decisions: list[ContextDecision], limit: int
    ) -> list[ContextItem]:
        all_groups = _group_groups(kept)
        droppable_groups: set[str] = set()
        for gkey, members in all_groups.items():
            if all(m.droppable and not m.required for m in members):
                droppable_groups.add(gkey)
        recalls = [
            i for i in kept if i.kind == "recalled_memory" and i.droppable and not i.required
        ]
        recalls.sort(key=lambda i: i.score if i.score is not None else 0.0)
        for item in recalls:
            if _tokens(kept) <= limit:
                break
            if item.group_id and item.group_id not in droppable_groups:
                continue  # 组内有 required/非可丢成员，保持完整
            if item.group_id:
                # 原子丢弃整组（方案 §8.1）
                for g in [i for i in kept if i.group_id == item.group_id]:
                    kept.remove(g)
                    self._drop(decisions, g, "recall_topk_reduce")
            elif item in kept:
                kept.remove(item)
                self._drop(decisions, item, "recall_topk_reduce")
        return kept

    # ---- helpers ----

    @staticmethod
    def _drop(decisions: list[ContextDecision], item: ContextItem, reason: str) -> None:
        decisions.append(
            ContextDecision(
                item_id=item.item_id,
                action="dropped",
                reason=reason,
                tokens_before=item.estimated_tokens,
                tokens_after=0,
            )
        )

    @staticmethod
    def _section_used(selected: list[ContextItem]) -> dict[str, int]:
        used: dict[str, int] = {}
        for item in selected:
            section = _section_for(item.kind)
            used[section] = used.get(section, 0) + item.estimated_tokens
        return used

    @staticmethod
    def _tokens_by_kind(selected: list[ContextItem]) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        for item in selected:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + item.estimated_tokens
        return by_kind


def build_budget(
    *,
    policy: ContextBudgetPolicy,
    context_window_tokens: int,
    reserved_output_tokens: int = 0,
    reserved_reasoning_tokens: int = 0,
) -> ContextBudget:
    """从 policy 与模型窗口构造 ``ContextBudget``（方案 §8.2 / §8.3）。"""
    tokens = compute_section_budget_tokens(
        policy,
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        reserved_reasoning_tokens=reserved_reasoning_tokens,
    )
    max_input = tokens["max_input_tokens"]
    # 小窗口（≤8K）动态调整：Prompt 占比提高到 30%，History 降到 25%
    if max_input <= 8192:
        from dataclasses import replace as _replace

        small_policy = _replace(
            policy,
            sections={
                "prompt": _replace(policy.sections["prompt"], percent=30, max_tokens=24000),
                "recent_history": _replace(
                    policy.sections["recent_history"],
                    percent=25,
                    max_tokens=64000,
                ),
            },
        )
        section_limits = {
            name: min(int(max_input * sb.percent / 100.0), sb.max_tokens)
            for name, sb in small_policy.sections.items()
        }
    else:
        section_limits = {
            name: min(int(max_input * sb.percent / 100.0), sb.max_tokens)
            for name, sb in policy.sections.items()
        }
    return ContextBudget(
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        reserved_reasoning_tokens=reserved_reasoning_tokens,
        safety_buffer_tokens=tokens["safety_buffer_tokens"],
        max_input_tokens=tokens["max_input_tokens"],
        soft_limit_tokens=tokens["soft_limit_tokens"],
        hard_limit_tokens=tokens["hard_limit_tokens"],
        section_limits=section_limits,
    )


def compute_section_budget_tokens(
    policy: ContextBudgetPolicy,
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
    reserved_reasoning_tokens: int,
) -> dict[str, int]:
    from ksadk.context_engine.policies import compute_budget_tokens

    return compute_budget_tokens(
        policy,
        context_window_tokens=context_window_tokens,
        reserved_output_tokens=reserved_output_tokens,
        reserved_reasoning_tokens=reserved_reasoning_tokens,
    )


__all__ = ["ContextPlanner", "build_budget"]
