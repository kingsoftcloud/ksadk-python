"""长任务固定数据集评测与发布门禁（长任务方案 §11 / P2 补强）。

已有指标函数（:mod:`ksadk.harness.metrics`）是纯函数；本模块把它们绑定到
**固定数据集 + 跨压缩真实执行**上，形成可重复的基线与发布门禁：

- :data:`FIXED_DATASET`：固定长任务用例（财务场景，历史中埋入关键事实：
  发票号 / 金额 / 日期 / 审批号），保证不同环境跑同一份数据可比较；
- :func:`run_case`：驱动 :class:`ManagedLangGraphEngine` 真实执行
  （含主动压缩），收集事件流；
- :func:`evaluate_long_task`：跨压缩指标汇总（压缩保真 + 关键事实保留 +
  Planned/Projected/Actual Token 闭环）；
- :func:`release_gate`：发布门禁——任一指标低于阈值即 fail（列出违规项）。

模型层说明：摘要质量由注入的 ``reasoner`` 决定。默认
:class:`FactPreservingReasoner` 是确定性摘要器（保留全部关键事实），
作为**基线**；评测劣化模型时传入丢弃事实的 reasoner 即可复现门禁失败。
真实模型 E2E 只需替换 reasoner 为 ``LiteLLMHarnessReasoner``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ksadk.harness.context_engine import extract_critical_facts
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.metrics import summarize_run
from ksadk.harness.observability import token_report

#: 默认门禁阈值（长任务方案 §11：关键指标全绿才可发布）。
DEFAULT_GATE_THRESHOLDS: dict[str, float] = {
    "goal_retention": 1.0,
    "constraint_retention": 1.0,
    "tool_pair_integrity": 1.0,
    "fact_retention": 1.0,
    "usage_manifest_paired_ratio": 1.0,
}


@dataclass(frozen=True)
class LongTaskCase:
    """固定数据集用例：长历史 + 埋入关键事实 + 触发跨压缩的小窗口。"""

    case_id: str
    user_input: str
    history: tuple[dict[str, str], ...]
    expected_facts: tuple[str, ...]
    context_window_tokens: int


def _finance_case(
    case_id: str, facts: tuple[tuple[str, str], ...], filler_turns: int
) -> LongTaskCase:
    """构造财务长任务用例：每个关键事实占一条历史，其余为填充轮次。"""
    history: list[dict[str, str]] = []
    for fact_id, fact_text in facts:
        history.append(
            {"role": "user", "content": f"请记录：{fact_id}，{fact_text}"}
        )
        history.append({"role": "assistant", "content": f"已记录 {fact_id}。"})
    for i in range(filler_turns):
        history.append(
            {"role": "user", "content": f"预算问题 {i} " + "背景细节" * 300}
        )
    expected = tuple(fact_id for fact_id, _ in facts)
    return LongTaskCase(
        case_id=case_id,
        user_input="汇总以上全部关键事实并给出结论。",
        history=tuple(history),
        expected_facts=expected,
        context_window_tokens=2048,
    )


#: 固定数据集（v1，冻结）：三个财务长任务用例，共 9 条关键事实。
FIXED_DATASET: tuple[LongTaskCase, ...] = (
    _finance_case(
        "finance-invoices",
        (
            ("INV-2026-0001", "金额 ¥12,300"),
            ("INV-2026-0002", "金额 ¥45,600"),
            ("AP-1024", "审批通过"),
        ),
        filler_turns=24,
    ),
    _finance_case(
        "finance-budget",
        (
            ("2026-Q3", "预算 ¥1,200,000"),
            ("2026-Q4", "预算 ¥1,350,000"),
            ("CMP-889", "超支 ¥86,000"),
        ),
        filler_turns=24,
    ),
    _finance_case(
        "finance-vendors",
        (
            ("VEN-3101", "供应商 华信科技"),
            ("CON-2026-77", "合同金额 ¥680,000"),
            ("DL-2026-09-30", "交付截止日"),
        ),
        filler_turns=24,
    ),
)


class FactPreservingReasoner:  # noqa: D101 - 见模块 docstring（基线摘要器）
    """确定性摘要 reasoner：摘要保留全部关键事实（基线）。

    实现 ``HarnessReasoner`` 协议（``complete``）：压缩器 prompt 的输入是
    历史，返回"关键事实全量列举 + 结论"形式的摘要，保证 Evidence
    Fidelity = 1.0 的**上界基线**——门禁失败只能来自管线（丢事实/
    事件缺失），而非摘要器。
    """

    async def complete(self, *, model, prompt, messages, tools):
        from ksadk.harness.reasoner import HarnessReasoningTurn

        text = "\n".join(str(m.get("content") or "") for m in messages)
        facts = sorted(extract_critical_facts(text))
        summary = "关键事实：\n" + "\n".join(facts) if facts else text[:2000]
        return HarnessReasoningTurn(final_text=summary)


class FactDroppingReasoner(FactPreservingReasoner):
    """劣化摘要 reasoner：丢弃一半关键事实（评测门禁的负样本）。"""

    async def complete(self, *, model, prompt, messages, tools):
        from ksadk.harness.reasoner import HarnessReasoningTurn

        text = "\n".join(str(m.get("content") or "") for m in messages)
        facts = sorted(extract_critical_facts(text))
        dropped = facts[: len(facts) // 2]
        summary = "关键事实：\n" + "\n".join(dropped) if dropped else text[:2000]
        return HarnessReasoningTurn(final_text=summary)


#: 引擎工厂：返回配好 reasoner 的 ManagedLangGraphEngine。
EngineFactory = Callable[[], Any]


def default_engine_factory(reasoner: Any | None = None) -> EngineFactory:
    """基线引擎工厂（HarnessContextEngine + 固定 reasoner）。"""
    from ksadk.harness.context_engine import HarnessContextEngine
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

    def factory() -> Any:
        return ManagedLangGraphEngine(
            reasoner=reasoner or FactPreservingReasoner(),
            context_engine=HarnessContextEngine(),
        )

    return factory


@dataclass
class CaseResult:
    """单用例执行结果：事件流 + 计算出的指标。"""

    case_id: str
    events: list[RuntimeEvent] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def run_case(
    case: LongTaskCase,
    engine_factory: EngineFactory | None = None,
    *,
    harness_spec_factory: Callable[[], Any] | None = None,
) -> CaseResult:
    """真实驱动一个用例：编译 → 启动 → 消费完整事件流（含压缩）。"""
    import asyncio

    from ksadk.runtime import StartRequest

    engine = (engine_factory or default_engine_factory())()

    if harness_spec_factory is not None:
        spec = harness_spec_factory()
    else:
        from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec

        spec = HarnessSpec(
            agent_revision_ref="agent-revision://eval@1",
            model=ModelBinding(profile_ref="model-profile://eval@1.0.0"),
            prompt=PromptSpec(instructions="你是财务分析助手。"),
        )

    request = StartRequest(
        agent_id="eval-agent",
        user_id="eval-user",
        session_id=f"eval-{case.case_id}",
        input=case.user_input,
        runtime_type="managed-langgraph",
        metadata={
            "conversation_history": [dict(m) for m in case.history],
            "context_window_tokens": case.context_window_tokens,
        },
    )

    async def drive() -> list[RuntimeEvent]:
        compiled = await engine.compile(spec)
        handle = await engine.start(request, compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    return CaseResult(case_id=case.case_id, events=events)


def fact_retention(case: LongTaskCase, events: Sequence[RuntimeEvent]) -> float:
    """跨压缩关键事实保留率：期望事实未被任何一次压缩丢弃的比例。"""
    dropped: set[str] = set()
    for event in events:
        if event.event_type != EventType.CONTEXT_COMPACTION_COMPLETED:
            continue
        dropped.update(str(f) for f in event.payload.get("dropped_critical_facts") or [])
    expected = set(case.expected_facts)
    if not expected:
        return 1.0
    return len(expected - dropped) / len(expected)


def usage_manifest_paired_ratio(events: Sequence[RuntimeEvent]) -> float:
    """Actual↔Manifest 配对率（闭环完整性）。

    只统计 **Manifest 驱动的模型调用** 的 usage 事件；压缩摘要调用
    （``purpose=compaction``）不对应任何 Manifest，其 Token 单独经
    :func:`compaction_usage_tokens` 归因，不掺入本指标。
    """
    usages = [
        e
        for e in events
        if e.event_type == EventType.USAGE_REPORTED
        and not e.payload.get("purpose")
    ]
    if not usages:
        return 1.0
    paired = [e for e in usages if e.payload.get("manifest_id")]
    return len(paired) / len(usages)


def compaction_usage_tokens(events: Sequence[RuntimeEvent]) -> dict[str, int]:
    """压缩摘要调用的 Token 开销（真实模型评测暴露的隐藏花费，单列归因）。"""
    compaction_usages = [
        e
        for e in events
        if e.event_type == EventType.USAGE_REPORTED and e.payload.get("purpose") == "compaction"
    ]
    return {
        "calls": len(compaction_usages),
        "input_tokens": sum(int(e.payload.get("input_tokens") or 0) for e in compaction_usages),
        "output_tokens": sum(int(e.payload.get("output_tokens") or 0) for e in compaction_usages),
    }


def evaluate_long_task(
    dataset: Sequence[LongTaskCase] = FIXED_DATASET,
    *,
    engine_factory: EngineFactory | None = None,
) -> dict[str, Any]:
    """固定数据集全量评测：逐用例执行 + 汇总指标（可重复基线）。"""
    factory = engine_factory or default_engine_factory()
    cases: list[dict[str, Any]] = []
    for case in dataset:
        result = run_case(case, factory)
        report = token_report(result.events)
        metrics = summarize_run(
            result.events,
            fact_retention=fact_retention(case, result.events),
            usage_manifest_paired_ratio=usage_manifest_paired_ratio(result.events),
            compaction_usage=compaction_usage_tokens(result.events),
            actual_total_input_tokens=report["actual_total_input_tokens"],
            actual_total_output_tokens=report["actual_total_output_tokens"],
        )
        metrics["compaction_count"] = metrics["compaction"]["compaction_count"]
        cases.append({"case_id": case.case_id, **metrics})
    return {
        "dataset": [c.case_id for c in dataset],
        "cases": cases,
        "aggregate": _aggregate(cases),
    }


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, float]:
    keys = (
        "goal_retention",
        "constraint_retention",
        "tool_pair_integrity",
        "fact_retention",
        "usage_manifest_paired_ratio",
    )
    agg: dict[str, float] = {}
    for key in keys:
        values = [
            c["compaction"].get(key, c.get(key, 1.0))
            if key in ("goal_retention", "constraint_retention", "tool_pair_integrity")
            else c.get(key, 1.0)
            for c in cases
        ]
        agg[key] = sum(values) / len(values) if values else 1.0
    return agg


def release_gate(
    report: dict[str, Any],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """发布门禁：聚合指标全部 ≥ 阈值才放行。

    返回 ``{"passed": bool, "violations": [{metric, actual, threshold}]}``。
    """
    limits = thresholds or DEFAULT_GATE_THRESHOLDS
    aggregate = report.get("aggregate") or {}
    violations = [
        {
            "metric": metric,
            "actual": aggregate.get(metric, 0.0),
            "threshold": threshold,
        }
        for metric, threshold in limits.items()
        if float(aggregate.get(metric, 0.0)) < threshold
    ]
    return {"passed": not violations, "violations": violations}


__all__ = [
    "DEFAULT_GATE_THRESHOLDS",
    "FactDroppingReasoner",
    "FactPreservingReasoner",
    "FIXED_DATASET",
    "LongTaskCase",
    "compaction_usage_tokens",
    "default_engine_factory",
    "evaluate_long_task",
    "fact_retention",
    "release_gate",
    "run_case",
    "usage_manifest_paired_ratio",
]
