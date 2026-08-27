"""真实模型 Skill 渐进披露 E2E 评测（L0→L1→L2→L3 自主演进验证）。

:mod:`tests.harness.test_skill_composition` 用脚本化 reasoner 验证装配；
本模块把 reasoner 换成**真实模型**（LiteLLM，Anthropic 兼容代理的 GLM），
验证模型在只有 L0 目录 + 三个受限披露工具的默认 Agent Loop 里能**自主**走完
渐进披露链路并基于最深披露层的信息作答。

每个用例 = 一个本地 Skill 包（SKILL.md 引用附属资源）+ 一个只有读到
Level 3 资源才能正确回答的任务。度量：

- **成功率**：最终答案包含资源内的关键事实（说明真到了 L3 并用上了）；
- **披露序列合规**：skill.disclosed 事件层级严格 1→2→3 递进（无跳级）；
- **越级访问**：模型未读 L1/L2 直接要 L2/L3（SkillDisclosureError 被
  Loop 拦截，计入越级次数，不产生 skill.disclosed 事件）；
- **无效披露**：其余失败的 Skill 工具调用（未绑定 skill_id、缺
  resource_ref、资源路径越界等）；
- **Token 消耗**：usage 事件聚合（input/output/total）。

运行方式（需要真实模型端点，不进默认测试套件）::

    KSADK_REAL_MODEL_EVAL=1 python -m ksadk.harness.skill_e2e_eval

模型配置与 :mod:`ksadk.harness.real_model_eval` 相同
（``KSADK_EVAL_LITELLM_MODEL`` / ``KSADK_EVAL_BASE_URL`` / ``KSADK_EVAL_API_KEY``）。
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.skill_composition import compose_engine
from ksadk.harness.skill_runtime import (
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
)
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)

_SKILL_TOOL_NAMES = frozenset({SKILL_MANIFEST_TOOL, SKILL_INSTRUCTIONS_TOOL, SKILL_RESOURCE_TOOL})


@dataclass(frozen=True)
class SkillE2ECase:
    """一个 Skill E2E 用例：Skill 包 + 只有 L3 信息才能答对的任务。"""

    case_id: str
    skill_ref: str
    skill_name: str
    description: str
    #: SKILL.md 正文（必须引用 ``resource_ref``，否则 L3 会被拒绝）。
    body: str
    resource_ref: str
    resource_content: str
    task: str
    #: 只有加载 resource 后才可知的关键事实（答案必须包含其中之一）。
    expected_facts: tuple[str, ...]


#: 固定数据集：三个领域各一个，均要求 L0→L1→L2→L3 全链路。
SKILL_E2E_DATASET: tuple[SkillE2ECase, ...] = (
    SkillE2ECase(
        case_id="budget-variance",
        skill_ref="skill://budget-variance@1.0.0",
        skill_name="预算偏差分析",
        description="分析预算与实际支出偏差并按标准口径输出结论",
        body=(
            "执行预算偏差分析的步骤：\n"
            "1. 询问或获取预算与实际支出数字；\n"
            "2. 读取 references/formula.md 获取公司标准偏差率计算口径；\n"
            "3. 按该口径计算并输出偏差率结论。\n"
        ),
        resource_ref="references/formula.md",
        resource_content=(
            "公司标准口径：偏差率 = (实际支出 - 预算) / 预算 × 100%，\n"
            "结论必须写成「偏差率=X%（口径：标准预算偏差率）」。\n"
            "口径代号：STD-BUDGET-VAR-2026。\n"
        ),
        task="预算 100 万，实际支出 120 万。请按公司标准口径给出偏差率结论，并注明口径代号。",
        expected_facts=("20%", "STD-BUDGET-VAR-2026"),
    ),
    SkillE2ECase(
        case_id="expense-policy",
        skill_ref="skill://expense-policy@1.0.0",
        skill_name="差旅报销政策",
        description="回答差旅报销政策相关问题",
        body=(
            "回答报销问题前必须读取 policy/limits.md 获取最新限额表，\n"
            "严禁凭记忆回答限额数字。\n"
        ),
        resource_ref="policy/limits.md",
        resource_content=(
            "2026 年限额表：经济舱单程上限 2500 元；住宿一线城市每晚上限 600 元；\n"
            "招待费人均上限 300 元；超出部分不予报销（政策编号 EXP-2026-11）。\n"
        ),
        task="我要去北京出差三晚，住一线城市酒店。请告诉我住宿每晚报销上限和政策编号。",
        expected_facts=("600", "EXP-2026-11"),
    ),
    SkillE2ECase(
        case_id="vendor-check",
        skill_ref="skill://vendor-check@1.0.0",
        skill_name="供应商准入核查",
        description="核查供应商准入状态与所需材料",
        body=(
            "核查供应商时先读取 checklist/materials.md 的最新材料清单，\n"
            "再回答供应商需要补齐哪些材料。\n"
        ),
        resource_ref="checklist/materials.md",
        resource_content=(
            "准入材料清单（V3）：营业执照、近两年审计报告、ISO9001 证书、\n"
            "无重大违法声明。清单版本号 CHK-V3；缺任一材料即暂停准入。\n"
        ),
        task="新供应商申请准入，请列出当前要求的全部材料并注明清单版本号。",
        expected_facts=("ISO9001", "CHK-V3"),
    ),
)


def _write_skill(case: SkillE2ECase, root: Path) -> None:
    skill_dir = root / case.skill_ref.split("//", 1)[1].replace("/", "-")
    resource = skill_dir / case.resource_ref
    resource.parent.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {case.skill_name}\ndescription: {case.description}\n---\n{case.body}",
        encoding="utf-8",
    )
    resource.write_text(case.resource_content, encoding="utf-8")


def _spec(case: SkillE2ECase) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref=f"agent-revision://{case.case_id}@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是企业助手，严格按已绑定 Skill 的说明完成任务。"),
        capabilities=CapabilityBindings(
            skill_bindings=(
                CapabilityBinding(
                    capability_ref=case.skill_ref, required=True, load_policy="on_demand"
                ),
            )
        ),
    )


@dataclass
class SkillE2ECaseReport:
    case_id: str
    success: bool
    levels_disclosed: list[int]
    sequence_valid: bool
    level_violations: int
    invalid_disclosures: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    final_answer: str
    detail: dict[str, Any] = field(default_factory=dict)


def analyze_case(
    case: SkillE2ECase, events: list[RuntimeEvent], final_answer: str
) -> SkillE2ECaseReport:
    """从事件流统计披露质量指标（纯函数，离线可测）。"""
    disclosed = [e for e in events if e.event_type == EventType.SKILL_DISCLOSED]
    levels = [int(e.payload.get("level")) for e in disclosed]  # type: ignore[arg-type]
    # 序列合规：层级 1→2→3 严格递进（重复读取允许但不得跳级）。
    ascending = all(
        levels[i + 1] - levels[i] in (0, 1) for i in range(len(levels) - 1)
    ) and (not levels or levels[0] == 1)
    level_violations = 0
    invalid = 0
    for event in events:
        if event.event_type != EventType.TOOL_CALL_END:
            continue
        if event.payload.get("name") not in _SKILL_TOOL_NAMES:
            continue
        error = str(event.payload.get("error") or "")
        if not error:
            continue
        if "须先披露" in error:
            level_violations += 1
        else:
            invalid += 1
    usage = [e for e in events if e.event_type == EventType.USAGE_REPORTED]
    input_tokens = sum(int(e.payload.get("input_tokens") or 0) for e in usage)
    output_tokens = sum(int(e.payload.get("output_tokens") or 0) for e in usage)
    answer = final_answer or ""
    success = any(fact in answer for fact in case.expected_facts)
    return SkillE2ECaseReport(
        case_id=case.case_id,
        success=success,
        levels_disclosed=levels,
        sequence_valid=ascending,
        level_violations=level_violations,
        invalid_disclosures=invalid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        final_answer=answer,
        detail={"facts_found": [f for f in case.expected_facts if f in answer]},
    )


async def run_case(case: SkillE2ECase, *, reasoner: Any, tmp_root: Path) -> SkillE2ECaseReport:
    """跑单个用例：装配 → 默认 Loop → 事件统计。"""
    from ksadk.runtime import StartRequest

    _write_skill(case, tmp_root)
    engine = compose_engine(_spec(case), reasoner=reasoner, local_dir=tmp_root)
    compiled = await engine.compile(_spec(case))
    handle = await engine.start(
        StartRequest(
            agent_id=f"skill-e2e-{case.case_id}",
            user_id="eval",
            session_id=f"eval-{case.case_id}",
            input=case.task,
            runtime_type="managed-langgraph",
        ),
        compiled,
    )
    final_answer = ""
    events: list[RuntimeEvent] = []
    async for event in engine.stream(handle):
        events.append(event)
        if event.event_type == EventType.TEXT_COMPLETED and event.phase == "final_answer":
            final_answer = str(event.payload.get("text") or "")
    return analyze_case(case, events, final_answer)


def run_skill_e2e(*, output_path: str = "") -> dict[str, Any]:
    """真实模型 Skill E2E 主入口（CLI / 测试复用）。"""
    from ksadk.harness.real_model_eval import RealModelReasoner

    reasoner = RealModelReasoner()
    reports: list[SkillE2ECaseReport] = []
    for case in SKILL_E2E_DATASET:
        with tempfile.TemporaryDirectory(prefix="ksadk-skill-e2e-") as tmp:
            reports.append(asyncio.run(run_case(case, reasoner=reasoner, tmp_root=Path(tmp))))

    aggregate = {
        "cases": len(reports),
        "success_rate": _ratio(r.success for r in reports),
        "sequence_valid_rate": _ratio(r.sequence_valid for r in reports),
        "max_level_reached": {
            "l1": sum(1 for r in reports if 1 in r.levels_disclosed),
            "l2": sum(1 for r in reports if 2 in r.levels_disclosed),
            "l3": sum(1 for r in reports if 3 in r.levels_disclosed),
        },
        "level_violations_total": sum(r.level_violations for r in reports),
        "invalid_disclosures_total": sum(r.invalid_disclosures for r in reports),
        "input_tokens": sum(r.input_tokens for r in reports),
        "output_tokens": sum(r.output_tokens for r in reports),
        "total_tokens": sum(r.total_tokens for r in reports),
    }
    report = {
        "aggregate": aggregate,
        "cases": [
            {
                "case_id": r.case_id,
                "success": r.success,
                "levels_disclosed": r.levels_disclosed,
                "sequence_valid": r.sequence_valid,
                "level_violations": r.level_violations,
                "invalid_disclosures": r.invalid_disclosures,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "total_tokens": r.total_tokens,
                "final_answer": r.final_answer[:400],
                "detail": r.detail,
            }
            for r in reports
        ],
    }
    if output_path:
        Path(output_path).write_text(_format_report(report), encoding="utf-8")
    return report


def _ratio(values: Any) -> float:
    items = list(values)
    return round(sum(1 for v in items if v) / len(items), 4) if items else 0.0


def _format_report(report: dict[str, Any]) -> str:
    lines = ["# 真实模型 Skill E2E 评测报告", ""]
    agg = report["aggregate"]
    lines.append(f"- 用例数：{agg['cases']}")
    lines.append(f"- 成功率：{agg['success_rate']:.0%}")
    lines.append(f"- 披露序列合规率：{agg['sequence_valid_rate']:.0%}")
    lines.append(f"- 披露到达层级：{agg['max_level_reached']}")
    lines.append(f"- 越级访问次数：{agg['level_violations_total']}")
    lines.append(f"- 无效披露次数：{agg['invalid_disclosures_total']}")
    lines.append(
        f"- Token 消耗：input={agg['input_tokens']} output={agg['output_tokens']} "
        f"total={agg['total_tokens']}"
    )
    lines.append("")
    for case in report["cases"]:
        lines.append(
            f"## {case['case_id']}：{'✅ 成功' if case['success'] else '❌ 失败'}"
            f"（levels={case['levels_disclosed']}, "
            f"越级={case['level_violations']}, 无效={case['invalid_disclosures']}, "
            f"tokens={case['total_tokens']}）"
        )
        lines.append(f"答案：{case['final_answer']}")
        lines.append(f"命中事实：{case['detail']['facts_found']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(description="真实模型 Skill 渐进披露 E2E 评测")
    parser.add_argument("--json-out", default="", help="原始 JSON 报告输出路径")
    parser.add_argument("--report-out", default="", help="Markdown 报告输出路径")
    args = parser.parse_args()
    if not os.getenv("KSADK_REAL_MODEL_EVAL"):
        raise SystemExit("需要 KSADK_REAL_MODEL_EVAL=1（真实模型端点）")
    report = run_skill_e2e(output_path=args.report_out)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    for case in report["cases"]:
        status = "✅" if case["success"] else "❌"
        print(
            f"{status} {case['case_id']}: levels={case['levels_disclosed']} "
            f"seq_ok={case['sequence_valid']} 越级={case['level_violations']} "
            f"无效={case['invalid_disclosures']} tokens={case['total_tokens']}"
        )
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


__all__ = [
    "SKILL_E2E_DATASET",
    "SkillE2ECase",
    "SkillE2ECaseReport",
    "analyze_case",
    "run_case",
    "run_skill_e2e",
]


if __name__ == "__main__":
    main()
