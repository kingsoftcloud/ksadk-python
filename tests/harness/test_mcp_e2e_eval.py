"""真实模型 MCP E2E 的离线回归（脚本化 reasoner 固定统计口径）。

真实模型端点跑法：``KSADK_REAL_MODEL_EVAL=1 python -m ksadk.harness.mcp_e2e_eval``。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.mcp_e2e_eval import (
    _FINANCE,
    _FINANCE_TOOLS,
    MCP_E2E_DATASET,
    _FakeTransport,
    analyze_case,
    run_case,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall


class _ScriptedReasoner:
    def __init__(self, calls: list[tuple[str, dict]], final_text: str = "完成。"):
        self._calls = calls
        self._final = final_text
        self.step = 0

    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, messages, tools
        if self.step < len(self._calls):
            name, arguments = self._calls[self.step]
            self.step += 1
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id=f"c{self.step}", name=name, arguments=arguments),
                )
            )
        return HarnessReasoningTurn(final_text=self._final)


def _good_calls() -> list[tuple[str, dict]]:
    return [
        ("mcp_list_tools", {"server_id": _FINANCE}),
        ("mcp_read_tool_schema", {"server_id": _FINANCE, "tool_name": "get_invoice"}),
        (
            "mcp_call_tool",
            {"server_id": _FINANCE, "tool_name": "get_invoice",
             "arguments": {"invoice_id": "INV-2026-0042"}},
        ),
    ]


def test_dataset_shape():
    assert len(MCP_E2E_DATASET) == 5
    assert {c.case_id for c in MCP_E2E_DATASET} == {
        "invoice-query", "high-risk-approval", "mcp-unavailable-degrade",
        "mixed-risk-dynamic-approval", "large-result-offload",
    }
    # 干扰工具规模足以体现预加载负担。
    assert len(_FINANCE_TOOLS) >= 20


def test_good_path_selection_success():
    case = MCP_E2E_DATASET[0]
    report = asyncio.run(
        run_case(case, reasoner=_ScriptedReasoner(_good_calls()),
                 preload_baseline_tokens=1000)
    )
    assert report.tool_selection_success
    assert report.level_violations == 0
    assert report.unauthorized_calls == 0
    assert report.unrelated_schema_loads == 0
    assert report.expected_outcome_met
    # Token 削减口径由真实模型回填（脚本 reasoner 无 usage 事件）。


def test_degrade_case_completes_without_run_failure():
    case = MCP_E2E_DATASET[2]
    report = asyncio.run(
        run_case(case, reasoner=_ScriptedReasoner(_good_calls()), preload_baseline_tokens=1000)
    )
    assert report.degraded_gracefully
    assert report.expected_outcome_met
    assert not report.tool_selection_success  # 传输层失败，调用未到达


def test_analyze_counts_unrelated_schema_and_violations():
    case = MCP_E2E_DATASET[0]
    transport = _FakeTransport(_FINANCE_TOOLS)
    events = [
        _event(EventType.MCP_DISCLOSED, {"server_id": _FINANCE, "level": 2,
                                         "tool_name": "list_budget_lines",
                                         "content_hash": "sha256:x", "size_bytes": 1}),
        _event(EventType.TOOL_CALL_END, {
            "call_id": "c1", "name": "mcp_call_tool",
            "error": "McpDisclosureError: mcp://finance-tools@1.0.0 的 Tool 'x' "
                     "须先 mcp_read_tool_schema 再调用",
        }),
    ]
    report = analyze_case(case, events, transport, "", preload_baseline_tokens=0)
    assert report.unrelated_schema_loads == 1
    assert report.level_violations == 1


def test_big_result_offload_case():
    """P1：大结果外置后调用仍正确，答案含金额与 artifact 引用即达成。"""
    case = next(c for c in MCP_E2E_DATASET if c.case_id == "large-result-offload")
    report = asyncio.run(
        run_case(
            case,
            reasoner=_ScriptedReasoner(
                _good_calls(), final_text="金额 88600，明细见 artifact://r/xxx"
            ),
            preload_baseline_tokens=1000,
        )
    )
    assert report.tool_selection_success
    assert report.expected_outcome_met


def _event(event_type: str, payload: dict[str, Any]) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="a", user_id="u", session_id="s", invocation_id="r", seq_id=1,
        payload=payload,
    )
