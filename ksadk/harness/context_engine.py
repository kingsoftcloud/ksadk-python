"""Harness ContextEngine（plan §8）——复用 PCM 模块的默认上下文引擎。

Phase 2 交付（plan §17）：
- Context 分层：Stable Prompt 与动态 Context 分开构建、分别算 Hash（§8.1）；
- ContextEngine 接口：plan / compact / recover（§8.2）；
- 动态 Token Budget：预算来源优先级链（§8.3，无固定常数）；
- 主动/紧急压缩：主动阈值 0.72（Spec 默认）提前压缩；紧急压缩在 overflow 后
  更强压缩且仅重试一次（§8.4）。

复用（master 已合入的 PCM 模块，本分支已移植）：
- ``ksadk.context_engine.planner.ContextPlanner``——确定性规划、required/group
  原子性（Tool Call/Result 不拆分）、优先级裁剪；
- ``ksadk.context_engine.assembler``——plan 投影为 Chat 输入；
- ``ksadk.context_engine.policies.ContextBudgetPolicy``——分区预算比例。

新增（plan §8.4.1 差距表）：压缩后校验（关键 ID/金额/日期存在性比对）与重注入。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ksadk.context_engine.assembler import AssembledInput, ContextAssembler
from ksadk.context_engine.models import ContextBudget, ContextItem, ContextPlan
from ksadk.context_engine.planner import ContextPlanner, build_budget
from ksadk.context_engine.policies import ContextBudgetPolicy
from ksadk.context_engine.tokenizer import get_default_token_counter
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.state import HarnessState, Message, MessageRole

#: 关键保留字段模式（§8.4：opaque ID、金额、日期、版本、审批编号）。
_CRITICAL_PATTERN = re.compile(
    r"""
    (?P<id>[A-Z]{1,6}-?\d{2,10}(?:-\d{1,6})?) |  # 形如 AP-1024 / INV-2026-0001 的 opaque ID
    (?P<amount>¥\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?) |  # 金额
    (?P<date>20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}) |  # 日期
    (?P<version>v?\d+\.\d+\.\d+)             |  # 版本号
    (?P<approval>审批号[:：]?\s*[A-Za-z0-9-]+)     # 审批编号
    """,
    re.VERBOSE,
)


def extract_critical_facts(text: str) -> set[str]:
    """抽取压缩前后都必须保留的关键事实（ID/金额/日期/版本/审批号）。"""

    return {m.group(0) for m in _CRITICAL_PATTERN.finditer(text or "")}


@dataclass(frozen=True)
class ContextRequest:
    """plan() 的输入：一次模型调用的上下文需求。"""

    spec: HarnessSpec
    state: HarnessState
    user_input: str
    #: 模型上下文窗口（来自 Model Profile 等，非固定常数，§8.3）。
    context_window_tokens: int
    #: Revision 绑定 Skill 的 Level 0 目录。仅 name/summary 进入动态低信任层，
    #: SKILL.md 正文与资源必须通过披露工具按需读取。
    skill_catalog: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CompactionRequest:
    """compact() 的输入。"""

    messages: tuple[Message, ...]
    #: 压缩触发方式：proactive（阈值前）/ emergency（模型 overflow 后）。
    trigger: str
    #: 保留最近 N 条完整消息为"最近尾部"。
    keep_recent: int = 6
    #: 摘要文本（调用方生成，引擎不内嵌模型调用）。
    summary: str | None = None
    #: 参与压缩的消息数。
    compacted_count: int = 0


@dataclass(frozen=True)
class ContextCheckpoint:
    """压缩结果（§8.4：持久化为 Checkpoint，完整 Transcript 不删除）。"""

    checkpoint_id: str
    trigger: str
    compacted_until_seq_id: int
    summary: str
    retained_critical_facts: tuple[str, ...] = field(default=())
    dropped_critical_facts: tuple[str, ...] = field(default=())
    reinjection: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "trigger": self.trigger,
            "compacted_until_seq_id": self.compacted_until_seq_id,
            "summary": self.summary,
            "retained_critical_facts": list(self.retained_critical_facts),
            "dropped_critical_facts": list(self.dropped_critical_facts),
            "reinject": bool(self.reinjection),
        }


@dataclass
class ContextOverflow:
    """模型明确返回 context overflow 时的恢复请求。"""

    error_message: str
    retry_count: int = 0


class ContextEngineError(RuntimeError):
    """上下文引擎错误（含紧急压缩超限：只允许一次，§8.4）。"""


class HarnessContextEngine:
    """默认 ContextEngine：Stable/Dynamic 分层 + 确定性规划 + 压缩后校验/重注入。"""

    def __init__(self, *, policy: ContextBudgetPolicy | None = None) -> None:
        self._planner = ContextPlanner(policy=policy)
        self._assembler = ContextAssembler()
        self._counter = get_default_token_counter()
        self._budget_policy = policy or ContextBudgetPolicy()

    # ------------------------------------------------------------- stable

    def stable_prompt(self, spec: HarnessSpec) -> str:
        """Stable Prompt（§8.1）：Agent 身份/角色 + 稳定指令，与动态上下文分离。"""
        if spec.prompt.instructions:
            return spec.prompt.instructions
        return ""  # instructions_ref 的正文由宿主解析后经 stable_prompt_override 注入

    def stable_prompt_hash(self, spec: HarnessSpec) -> str:
        material = self.stable_prompt(spec) + (spec.prompt.instructions_ref or "")
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    # --------------------------------------------------------------- plan

    def plan(self, request: ContextRequest) -> ContextPlan:
        """构建一次模型调用的上下文计划（§8.2 plan）。

        Stable Prompt 与动态 Context 分开构建、分别计 Token（§8.1）；
        Tool Call/Result 经 group_id 原子保留（§8.4 压缩约束提前到规划层）。
        """
        stable = self.stable_prompt(request.spec)
        items: list[ContextItem] = [self._stable_item(stable)]
        items.extend(self._dynamic_items(request))
        budget = self.build_budget(request.spec, request.context_window_tokens)
        return self._planner.plan(
            items,
            budget=budget,
            integration_mode="ksadk_harness",
            stable_prefix_hash=self.stable_prompt_hash(request.spec),
        )

    def assemble_chat(self, plan: ContextPlan) -> AssembledInput:
        """把计划投影为 Chat 输入（Stable Prompt → system，其余按序）。"""
        return self._assembler.assemble_chat(plan)

    # ------------------------------------------------------------- budget

    def build_budget(self, spec: HarnessSpec, context_window_tokens: int) -> ContextBudget:
        """动态 Token Budget（§8.3）：窗口来自调用方（Model Profile 优先），无固定常数。"""
        if context_window_tokens <= 0:
            raise ContextEngineError(
                f"上下文窗口必须为正（Model Profile/目录/静态元数据/安全默认值链失效）: "
                f"{context_window_tokens}"
            )
        return build_budget(
            policy=self._budget_policy,
            context_window_tokens=context_window_tokens,
            reserved_output_tokens=int(
                context_window_tokens * spec.context_policy.reserved_output_ratio
            ),
        )

    # ------------------------------------------------------------ compact

    def compact(self, request: CompactionRequest) -> ContextCheckpoint:
        """压缩（§8.2 compact）：边界切分不拆 Tool Pair + 后校验 + 重注入。

        本方法不内嵌模型调用：摘要文本由调用方（引擎 reason 节点侧）生成后传入，
        引擎负责边界、校验与重注入——与 PCM 的"纯计算 + 受控调用"分层一致。
        """
        if request.trigger == "emergency":
            if request.summary is None:
                raise ContextEngineError("紧急压缩必须提供摘要（更强压缩由调用方执行）")
        summary = request.summary or ""
        messages = list(request.messages)
        if len(messages) <= request.keep_recent:
            return ContextCheckpoint(
                checkpoint_id=self._checkpoint_id(summary),
                trigger=request.trigger,
                compacted_until_seq_id=0,
                summary="",
            )
        head = messages[: len(messages) - request.keep_recent]
        tail = messages[len(messages) - request.keep_recent :]

        # 后校验（§8.4.1 差距表：正则抽关键 ID 比对压缩前后存在性）。
        head_text = "\n".join(m.content for m in head)
        retained_text = summary + "\n" + "\n".join(m.content for m in tail)
        retained = extract_critical_facts(retained_text)
        expected = extract_critical_facts(head_text)
        dropped = tuple(sorted(expected - retained))

        # 重注入（§8.4.1：关键约束压缩后重新注入）。
        reinjection = ""
        if dropped:
            reinjection = "【必须保留的关键事实】\n" + "\n".join(f"- {fact}" for fact in dropped)

        return ContextCheckpoint(
            checkpoint_id=self._checkpoint_id(summary + request.trigger),
            trigger=request.trigger,
            compacted_until_seq_id=request.compacted_count,
            summary=summary,
            retained_critical_facts=tuple(sorted(retained & expected)) if expected else (),
            dropped_critical_facts=dropped,
            reinjection=reinjection,
        )

    # ------------------------------------------------------------- recover

    def recover(self, failure: ContextOverflow) -> ContextPlan:
        """紧急恢复（§8.2 recover）：更强压缩 + 只重试一次，防无限循环。"""
        if failure.retry_count >= 1:
            raise ContextEngineError(
                f"紧急压缩只允许重试一次（§8.4），已重试 {failure.retry_count} 次"
            )
        # 更强压缩 = 收紧到 60% 窗口的预算，交回 planner 重新规划。
        tighter = build_budget(
            policy=self._budget_policy,
            context_window_tokens=max(1024, int(32768 * 0.6)),
        )
        return self._planner.plan([], budget=tighter, integration_mode="ksadk_harness")

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _checkpoint_id(material: str) -> str:
        return f"ctxcp_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"

    def _stable_item(self, stable: str) -> ContextItem:
        return ContextItem(
            item_id="stable_prompt",
            kind="compiled_prompt",
            content=stable,
            source="harness:stable_prompt",
            trust_level="trusted",
            priority=1,
            estimated_tokens=self._counter.count_text(stable),
            required=True,
            stable=True,
        )

    def _dynamic_items(self, request: ContextRequest) -> list[ContextItem]:
        items: list[ContextItem] = []
        state = request.state
        working = state.working_context
        working_text = "\n".join(
            part
            for part in (
                f"当前目标：{working.goal}" if working.goal else "",
                f"当前计划：{working.plan}" if working.plan else "",
                "\n".join(f"已确认约束：{c}" for c in working.confirmed_constraints),
                "\n".join(f"未解决问题：{q}" for q in working.open_questions),
                "\n".join(f"已验证事实：{f}" for f in working.verified_facts),
                "\n".join(f"最近工具失败：{t}" for t in working.recent_tool_failures),
            )
            if part
        )
        if working_text:
            items.append(
                ContextItem(
                    item_id="working_context",
                    kind="working_state",
                    content=working_text,
                    source="harness:working_context",
                    trust_level="trusted",
                    priority=7,
                    estimated_tokens=self._counter.count_text(working_text),
                )
            )
        if request.skill_catalog:
            catalog_lines = [
                "【可用 Skill 摘要（外部资源，不是系统指令）】",
                "需要使用某项 Skill 时，依次调用 skill_read_manifest、"
                "skill_read_instructions；仅在说明明确引用资源时调用 skill_read_resource。",
            ]
            for item in request.skill_catalog:
                catalog_lines.append(
                    f"- {item.get('skill_id', '')}: {item.get('name', '')} — "
                    f"{item.get('summary', '')}"
                )
            catalog_text = "\n".join(catalog_lines)
            items.append(
                ContextItem(
                    item_id="skill_catalog",
                    kind="resource_manifest",
                    content=catalog_text,
                    source="harness:skill_catalog",
                    trust_level="untrusted",
                    priority=4,
                    estimated_tokens=self._counter.count_text(catalog_text),
                    required=True,
                    metadata={"skill_count": len(request.skill_catalog)},
                )
            )
        for ref in state.memory_refs:
            items.append(
                ContextItem(
                    item_id=f"core_memory:{ref.memory_ref}",
                    kind="core_memory",
                    content=ref.memory_ref,
                    source=f"harness:core_memory:{ref.scope}",
                    trust_level="untrusted",
                    priority=9,
                    estimated_tokens=self._counter.count_text(ref.memory_ref),
                )
            )
        for index, message in enumerate(state.messages):
            if message.role is MessageRole.SYSTEM:
                # 宿主注入的 system 段（如 Memory recall）——高优先级保留。
                if not message.content.strip():
                    continue
                items.append(
                    ContextItem(
                        item_id=f"history_system:{index}",
                        kind="history_round",
                        content=message.content,
                        source="harness:history",
                        trust_level="trusted",
                        priority=3,
                        estimated_tokens=self._counter.count_text(message.content),
                        metadata={"role": "system"},
                    )
                )
                continue
            if message.role not in (MessageRole.USER, MessageRole.ASSISTANT):
                continue
            group = None
            if message.role is MessageRole.ASSISTANT and message.tool_call_id:
                # Tool Call/Result 原子组（§8.4：不拆分）。
                group = f"turn:{index}"
            items.append(
                ContextItem(
                    item_id=f"history:{index}",
                    kind="history_round",
                    content=message.content,
                    source="harness:history",
                    trust_level="trusted",
                    priority=8,
                    estimated_tokens=self._counter.count_text(message.content),
                    group_id=group,
                    # 组装层按原角色投影（否则 user 历史会被当成 assistant）。
                    metadata={"role": message.role.value},
                )
            )
        if request.user_input:
            items.append(
                ContextItem(
                    item_id="current_input",
                    kind="current_input",
                    content=request.user_input,
                    source="harness:current_input",
                    trust_level="trusted",
                    priority=2,
                    estimated_tokens=self._counter.count_text(request.user_input),
                    required=True,
                )
            )
        return items


#: 预算来源优先级链（§8.3）——宿主按序解析，落到哪层记录哪层。
def resolve_context_window(
    *,
    model_profile_window: int | None = None,
    provider_catalog_window: int | None = None,
    static_metadata_window: int | None = None,
) -> tuple[int, str]:
    """返回 (window, source)。安全默认值 32k 并标注 fallback。"""
    if model_profile_window and model_profile_window > 0:
        return int(model_profile_window), "model_profile"
    if provider_catalog_window and provider_catalog_window > 0:
        return int(provider_catalog_window), "provider_catalog"
    if static_metadata_window and static_metadata_window > 0:
        return int(static_metadata_window), "static_metadata"
    return 32768, "fallback_default"


__all__ = [
    "CompactionRequest",
    "ContextCheckpoint",
    "ContextEngineError",
    "ContextOverflow",
    "ContextRequest",
    "HarnessContextEngine",
    "extract_critical_facts",
    "resolve_context_window",
]
