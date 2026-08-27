"""真实模型 MCP 渐进披露 E2E 评测（P0 Deferred Tool Loading 验收）。

与 :mod:`ksadk.harness.skill_e2e_eval` 对齐：真实模型（LiteLLM，Anthropic
兼容代理的 GLM）在只注入 L0 MCP Server 目录 + 三个受控披露工具的默认
Agent Loop 里，自主完成「选 Server → 列 Tool → 读 Schema → 调用」。

验收指标（对应 P0 验收）：

- **Tool 选择成功率**：L3 调用到达正确的 (server, tool) 且参数正确；
- **越级/未授权调用**：跳过 Schema 的调用被 Loop 拦截，计 0 次成功越级；
- **无关 Schema 加载**：L2 披露了目标任务之外的 Tool 计为无关加载；
- **首轮输入 Token 削减**：对比「全量预加载所有 Tool Schema」基线；
- **审批**：高风险 Server 的调用必须先进 approval.requested（不执行）；
- **降级**：MCP 不可用时 Run 不失败（工具错误可见，模型可正常收尾）。

运行方式（需要真实模型端点，不进默认测试套件）::

    KSADK_REAL_MODEL_EVAL=1 python -m ksadk.harness.mcp_e2e_eval

模型配置与 :mod:`ksadk.harness.real_model_eval` 相同。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ksadk.harness.capabilities import CapabilityDescriptor, RiskLevel
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.mcp_runtime import (
    McpCapabilityRuntime,
    McpServerBinding,
    McpTransport,
)
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.runtime import StartRequest

_FINANCE = "mcp://finance-tools@1.0.0"
_HR = "mcp://hr-tools@1.0.0"

_FINANCE_TOOLS: dict[str, dict] = {
    "get_invoice": {
        "name": "get_invoice",
        "description": "按发票号查询发票金额与状态",
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string", "description": "发票号"}},
            "required": ["invoice_id"],
        },
    },
}
# 模拟真实 MCP Server：几十个与任务无关的 Tool（全量预加载的负担来源）。
_FILLER_TOOL_NAMES = (
    "list_budget_lines", "create_expense_report", "approve_expense_report",
    "reject_expense_report", "get_budget_summary", "list_vendors",
    "create_vendor", "update_vendor", "deactivate_vendor",
    "get_payment_status", "schedule_payment", "cancel_payment",
    "reconcile_statement", "export_ledger", "import_ledger",
    "get_tax_rate", "create_tax_record", "list_tax_records",
    "get_department_spend", "forecast_quarterly_spend",
    "allocate_budget", "transfer_budget", "close_fiscal_period",
    "generate_vat_invoice", "void_vat_invoice",
)
for _name in _FILLER_TOOL_NAMES:
    _FINANCE_TOOLS[_name] = {
        "name": _name,
        "description": f"财务系统操作：{_name}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "department": {"type": "string", "description": "部门编码"},
                "period": {"type": "string", "description": "会计期间 YYYY-MM"},
                "document_id": {"type": "string", "description": "单据编号"},
                "amount": {"type": "number", "description": "金额"},
                "currency": {"type": "string", "description": "币种，默认 CNY"},
                "memo": {"type": "string", "description": "备注"},
            },
            "required": ["department", "period"],
        },
    }
_HR_TOOLS = {
    "get_employee": {
        "name": "get_employee",
        "description": "按工号查询员工档案（与发票无关，干扰项）",
        "inputSchema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string"}},
            "required": ["employee_id"],
        },
    },
}


class _FakeTransport(McpTransport):
    def __init__(self, tools: dict[str, dict], *, fail: bool = False) -> None:
        self._tools = tools
        self._fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._fail:
            raise ConnectionError("mcp server unreachable")
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._fail:
            raise ConnectionError("mcp server unreachable")
        self.calls.append((name, dict(arguments)))
        if name == "get_invoice" and arguments.get("invoice_id") == "INV-2026-0042":
            return {"invoice_id": "INV-2026-0042", "amount": 88600, "currency": "CNY",
                    "status": "approved", "vendor": "华信科技"}
        return {"status": "ok", "tool": name, "echo": arguments}


@dataclass(frozen=True)
class McpE2ECase:
    case_id: str
    task: str
    finance_risk: RiskLevel = RiskLevel.MEDIUM
    finance_fail: bool = False
    #: 期望命中的 (server_id, tool_name, 关键参数)。
    expect_call: tuple[str, str, dict[str, Any]] | None = None
    #: 答案必须包含的事实（来自工具结果）。
    expected_facts: tuple[str, ...] = ()
    #: 预期行为：run_completed（正常/降级收尾）或 awaiting_approval。
    expect_outcome: str = "run_completed"


#: 固定数据集：多 Server 竞争 + 相近 Tool 名干扰 + 高风险审批 + 不可用降级。
MCP_E2E_DATASET: tuple[McpE2ECase, ...] = (
    McpE2ECase(
        case_id="invoice-query",
        task="查一下发票 INV-2026-0042 的金额和状态，只查这一张，不要查别的。",
        expect_call=(_FINANCE, "get_invoice", {"invoice_id": "INV-2026-0042"}),
        expected_facts=("88600",),
    ),
    McpE2ECase(
        case_id="high-risk-approval",
        task="请支付发票 INV-2026-0042，金额按查询结果为准。",
        finance_risk=RiskLevel.HIGH,
        expect_call=(_FINANCE, "get_invoice", {"invoice_id": "INV-2026-0042"}),
        expect_outcome="awaiting_approval",
    ),
    McpE2ECase(
        case_id="mcp-unavailable-degrade",
        task="查一下发票 INV-2026-0042 的金额和状态。",
        finance_fail=True,
        expect_outcome="run_completed",
    ),
)


def _runtime(case: McpE2ECase) -> tuple[McpCapabilityRuntime, _FakeTransport]:
    transport = _FakeTransport(_FINANCE_TOOLS, fail=case.finance_fail)
    hr_transport = _FakeTransport(_HR_TOOLS)
    runtime = McpCapabilityRuntime()
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_FINANCE, kind="mcp", name="财务工具",
                description="发票查询、支付与预算管理", version="1.0.0",
                risk_level=case.finance_risk,
            ),
            transport=transport, required=True,
        )
    )
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=_HR, kind="mcp", name="人事工具",
                description="员工档案与考勤查询", version="1.0.0",
            ),
            transport=hr_transport, required=False,
        )
    )
    return runtime, transport


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是企业助手，用已绑定的 MCP 工具完成任务。"),
        capabilities=CapabilityBindings(
            mcp_bindings=(
                CapabilityBinding(capability_ref=_FINANCE, required=True, load_policy="on_demand"),
                CapabilityBinding(capability_ref=_HR, required=False, load_policy="on_demand"),
            )
        ),
    )


@dataclass
class McpE2ECaseReport:
    case_id: str
    tool_selection_success: bool
    level_violations: int
    unauthorized_calls: int
    unrelated_schema_loads: int
    first_input_tokens: int
    preload_baseline_tokens: int
    token_reduction: float
    approval_entered: bool
    degraded_gracefully: bool
    expected_outcome_met: bool
    final_answer: str
    detail: dict[str, Any] = field(default_factory=dict)


def analyze_case(
    case: McpE2ECase,
    events: list[RuntimeEvent],
    transport: _FakeTransport,
    final_answer: str,
    *,
    preload_baseline_tokens: int,
) -> McpE2ECaseReport:
    """从事件流统计 P0 验收指标（纯函数，离线可测）。"""
    disclosed = [e for e in events if e.event_type == EventType.MCP_DISCLOSED]
    l2 = [e for e in disclosed if e.payload["level"] == 2]
    l3 = [e for e in disclosed if e.payload["level"] == 3]
    violations = sum(
        1 for e in events
        if e.event_type == EventType.TOOL_CALL_END
        and e.payload.get("name") in ("mcp_list_tools", "mcp_read_tool_schema", "mcp_call_tool")
        and "须先" in str(e.payload.get("error") or "")
    )
    unrelated = 0
    if case.expect_call is not None:
        wanted_server, wanted_tool, _ = case.expect_call
        unrelated = sum(
            1 for e in l2
            if not (e.payload["server_id"] == wanted_server
                    and e.payload.get("tool_name") == wanted_tool)
        )
    # 成功越级：L3 披露存在但对应 (server, tool) 从未 L2 → 结构上不可能
    # （bridge 强制），这里统计 L3 调用数减去有 L2 支撑的调用数。
    schema_read = {
        (e.payload["server_id"], e.payload.get("tool_name")) for e in l2
    }
    unauthorized = sum(
        1 for e in l3
        if (e.payload["server_id"], e.payload.get("tool_name")) not in schema_read
    )
    if case.expect_call is not None:
        server, tool, args = case.expect_call
        matched = any(
            name == tool and all(arguments.get(k) == v for k, v in args.items())
            for name, arguments in transport.calls
        ) and transport.calls
    else:
        matched = False
    usage = [e for e in events if e.event_type == EventType.USAGE_REPORTED]
    first_input = int(usage[0].payload.get("input_tokens") or 0) if usage else 0
    approval_entered = any(e.event_type == EventType.APPROVAL_REQUESTED for e in events)
    degraded = case.finance_fail and any(
        e.event_type == EventType.TOOL_CALL_END and e.payload.get("error")
        for e in events
    ) and not any(
        e.event_type == EventType.RUN_FAILED for e in events
    )
    if case.expect_outcome == "awaiting_approval":
        outcome_met = approval_entered and not any(
            e.event_type == EventType.RUN_COMPLETED for e in events
        )
    elif case.finance_fail:
        outcome_met = degraded
    else:
        outcome_met = matched and any(e.event_type == EventType.RUN_COMPLETED for e in events)
    reduction = (
        round(1 - first_input / preload_baseline_tokens, 4)
        if preload_baseline_tokens and first_input
        else 0.0
    )
    return McpE2ECaseReport(
        case_id=case.case_id,
        tool_selection_success=matched,
        level_violations=violations,
        unauthorized_calls=unauthorized,
        unrelated_schema_loads=unrelated,
        first_input_tokens=first_input,
        preload_baseline_tokens=preload_baseline_tokens,
        token_reduction=reduction,
        approval_entered=approval_entered,
        degraded_gracefully=degraded,
        expected_outcome_met=outcome_met,
        final_answer=final_answer,
        detail={
            "l3_calls": [
                (e.payload["server_id"], e.payload.get("tool_name")) for e in l3
            ],
            "transport_calls": transport.calls,
        },
    )


class _DirectTool:
    """把 MCP Tool Schema 直接暴露给模型（全量预加载基线用）。"""

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec

    @property
    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": self._spec}

    async def call(self, arguments: dict[str, Any]) -> Any:
        return {"status": "ok", "tool": self._spec["name"], "echo": arguments}


async def measure_preload_baseline(*, reasoner: Any) -> int:
    """全量预加载基线：同样的任务，所有 MCP Tool Schema 直接作为工具注入，
    取真实模型首轮输入 Token（apples-to-apples，对比 deferred 首轮）。"""
    tools = {
        spec["name"]: _DirectTool(spec)
        for spec in list(_FINANCE_TOOLS.values()) + list(_HR_TOOLS.values())
    }
    engine = ManagedLangGraphEngine(reasoner=reasoner, tools=tools)
    compiled = await engine.compile(_spec_without_mcp())
    handle = await engine.start(
        StartRequest(
            agent_id="mcp-e2e-preload",
            user_id="eval",
            session_id="eval-preload",
            input=MCP_E2E_DATASET[0].task,
            runtime_type="managed-langgraph",
        ),
        compiled,
    )
    async for event in engine.stream(handle):
        if event.event_type == EventType.USAGE_REPORTED:
            await engine.close(handle)
            return int(event.payload.get("input_tokens") or 0)
    await engine.close(handle)
    return 0


def _spec_without_mcp() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://finance@1",
        model=ModelBinding(profile_ref="model-profile://test@1.0.0"),
        prompt=PromptSpec(instructions="你是企业助手，用已绑定的工具完成任务。"),
    )


async def run_case(
    case: McpE2ECase, *, reasoner: Any, preload_baseline_tokens: int = 0
) -> McpE2ECaseReport:
    runtime, transport = _runtime(case)
    engine = ManagedLangGraphEngine(reasoner=reasoner, mcp_runtime=runtime)
    compiled = await engine.compile(_spec())
    handle = await engine.start(
        StartRequest(
            agent_id=f"mcp-e2e-{case.case_id}",
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
    return analyze_case(
        case, events, transport, final_answer,
        preload_baseline_tokens=preload_baseline_tokens,
    )


def run_mcp_e2e(*, output_path: str = "") -> dict[str, Any]:
    from ksadk.harness.real_model_eval import RealModelReasoner

    reasoner = RealModelReasoner()
    baseline = asyncio.run(measure_preload_baseline(reasoner=reasoner))
    reports = [
        asyncio.run(run_case(case, reasoner=reasoner, preload_baseline_tokens=baseline))
        for case in MCP_E2E_DATASET
    ]
    actionable = [r for r in reports if r.case_id == "invoice-query"]
    aggregate = {
        "cases": len(reports),
        "tool_selection_success_rate": _ratio(r.tool_selection_success for r in actionable),
        "level_violations_total": sum(r.level_violations for r in reports),
        "unauthorized_calls_total": sum(r.unauthorized_calls for r in reports),
        "unrelated_schema_loads_total": sum(r.unrelated_schema_loads for r in reports),
        "first_input_tokens_deferred": (
            actionable[0].first_input_tokens if actionable else 0
        ),
        "preload_baseline_tokens": (
            actionable[0].preload_baseline_tokens if actionable else 0
        ),
        "first_input_token_reduction": (
            actionable[0].token_reduction if actionable else 0.0
        ),
        "approval_entered": any(r.approval_entered for r in reports),
        "degraded_gracefully": any(r.degraded_gracefully for r in reports),
        "outcome_met_rate": _ratio(r.expected_outcome_met for r in reports),
    }
    report = {
        "aggregate": aggregate,
        "cases": [
            {
                "case_id": r.case_id,
                "tool_selection_success": r.tool_selection_success,
                "level_violations": r.level_violations,
                "unauthorized_calls": r.unauthorized_calls,
                "unrelated_schema_loads": r.unrelated_schema_loads,
                "first_input_tokens": r.first_input_tokens,
                "token_reduction": r.token_reduction,
                "approval_entered": r.approval_entered,
                "degraded_gracefully": r.degraded_gracefully,
                "expected_outcome_met": r.expected_outcome_met,
                "final_answer": r.final_answer[:400],
                "detail": {
                    "l3_calls": [[s, t] for s, t in r.detail["l3_calls"]],
                    "transport_calls": [
                        [n, a] for n, a in r.detail["transport_calls"]
                    ],
                },
            }
            for r in reports
        ],
    }
    if output_path:
        Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return report


def _ratio(values: Any) -> float:
    items = list(values)
    return round(sum(1 for v in items if v) / len(items), 4) if items else 0.0


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="真实模型 MCP 渐进披露 E2E 评测")
    parser.add_argument("--json-out", default="", help="JSON 报告输出路径")
    args = parser.parse_args()
    if not os.getenv("KSADK_REAL_MODEL_EVAL"):
        raise SystemExit("需要 KSADK_REAL_MODEL_EVAL=1（真实模型端点）")
    report = run_mcp_e2e(output_path=args.json_out)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    for case in report["cases"]:
        status = "✅" if case["expected_outcome_met"] else "❌"
        print(
            f"{status} {case['case_id']}: selection={case['tool_selection_success']} "
            f"越级={case['level_violations']} 未授权={case['unauthorized_calls']} "
            f"无关Schema={case['unrelated_schema_loads']} "
            f"首轮tokens={case['first_input_tokens']} 削减={case['token_reduction']:.0%}"
        )


__all__ = [
    "MCP_E2E_DATASET",
    "McpE2ECase",
    "McpE2ECaseReport",
    "analyze_case",
    "measure_preload_baseline",
    "run_case",
    "run_mcp_e2e",
]


if __name__ == "__main__":
    main()
