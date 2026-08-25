"""Harness Conformance — 事件契约校验（plan §15）。

Conformance 不要求所有 Engine 原生能力相同，而是验证：相同外部契约、
事件语义一致、Tool Call 成对、不支持的能力诚实声明。本模块是纯函数
校验器，可对任意 RuntimeAdapter 的 event 流执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ksadk.events import EventType, RuntimeEvent

_TERMINAL_EVENTS = frozenset(
    {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELED}
)


@dataclass
class ConformanceViolation:
    rule: str
    detail: str


@dataclass
class ConformanceReport:
    violations: list[ConformanceViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def fail(self, rule: str, detail: str) -> None:
        self.violations.append(ConformanceViolation(rule=rule, detail=detail))


def verify_start_and_terminal_event(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """run.started 必须是首事件；必须以恰好一个终止事件收尾。"""
    if not events:
        report.fail("lifecycle", "事件流为空")
        return
    if events[0].event_type != EventType.RUN_STARTED:
        report.fail("lifecycle", f"首事件必须是 run.started，实际 {events[0].event_type}")
    terminal = [e for e in events if e.event_type in _TERMINAL_EVENTS]
    if len(terminal) != 1:
        kinds = [e.event_type for e in terminal]
        report.fail("lifecycle", f"终止事件必须恰好一个，实际 {len(terminal)}: {kinds}")
    elif events[-1].event_type not in _TERMINAL_EVENTS:
        report.fail("lifecycle", "终止事件之后不允许再出现事件")


def verify_event_ordering(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """seq_id 严格递增；同一 invocation 内信封字段一致。"""
    for prev, cur in zip(events, events[1:]):
        if cur.seq_id <= prev.seq_id:
            report.fail(
                "ordering",
                f"seq_id 必须严格递增: {prev.seq_id} -> {cur.seq_id} ({cur.event_type})",
            )
    for field_name in ("agent_id", "user_id", "session_id", "invocation_id"):
        values = {getattr(e, field_name) for e in events}
        if len(values) > 1:
            report.fail("ordering", f"信封字段 {field_name} 在流内不一致: {values}")
    if events and events[-1].event_type == EventType.RUN_COMPLETED:
        last_text_idx = max(
            (i for i, e in enumerate(events) if e.event_type == EventType.TEXT_COMPLETED),
            default=None,
        )
        if last_text_idx is not None:
            terminal_idx = len(events) - 1
            if last_text_idx > terminal_idx:
                report.fail("ordering", "text.completed 出现在终止事件之后")


def verify_tool_call_pairing(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """tool.call.begin/end 按 call_id 成对且 begin 在前。"""
    seen_begin: set[str] = set()
    closed: set[str] = set()
    for event in events:
        if event.event_type == EventType.TOOL_CALL_BEGIN:
            call_id = str(event.payload.get("call_id", ""))
            if not call_id:
                report.fail("tool-pair", "tool.call.begin 缺少 call_id")
            elif call_id in seen_begin:
                report.fail("tool-pair", f"call_id 重复 begin: {call_id}")
            else:
                seen_begin.add(call_id)
        elif event.event_type == EventType.TOOL_CALL_END:
            call_id = str(event.payload.get("call_id", ""))
            if not call_id:
                report.fail("tool-pair", "tool.call.end 缺少 call_id")
            elif call_id not in seen_begin:
                report.fail("tool-pair", f"tool.call.end 无对应 begin: {call_id}")
            elif call_id in closed:
                report.fail("tool-pair", f"call_id 重复 end: {call_id}")
            else:
                closed.add(call_id)
    for call_id in sorted(seen_begin - closed):
        report.fail("tool-pair", f"tool.call.begin 无对应 end: {call_id}")


def verify_secret_redaction(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """payload 中不允许出现疑似密钥字段值（保守子串匹配）。"""
    sensitive_keys = ("api_key", "apikey", "authorization", "secret", "password", "credential")

    def scan(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_s = str(key).lower()
                if key_s in sensitive_keys and item not in (None, "", "***"):
                    report.fail("redaction", f"payload 泄漏敏感字段 {path}.{key}")
                else:
                    scan(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                scan(item, f"{path}[{index}]")

    for event in events:
        scan(event.payload, event.event_type)


def verify_cancel_honesty(
    events: list[RuntimeEvent],
    report: ConformanceReport,
    *,
    cancel_requested: bool,
) -> None:
    """请求过 Cancel 的流必须以 run.canceled 收尾（不许伪装成 completed）。"""
    if not cancel_requested:
        return
    terminal = events[-1].event_type if events else None
    if terminal != EventType.RUN_CANCELED:
        report.fail("cancel-honesty", f"Cancel 后终止事件必须是 run.canceled，实际 {terminal}")


def run_conformance_suite(
    events: list[RuntimeEvent],
    *,
    cancel_requested: bool = False,
) -> ConformanceReport:
    """Phase 0 最小套件入口：5 项校验一次跑完。"""
    report = ConformanceReport()
    verify_start_and_terminal_event(events, report)
    verify_event_ordering(events, report)
    verify_tool_call_pairing(events, report)
    verify_secret_redaction(events, report)
    verify_cancel_honesty(events, report, cancel_requested=cancel_requested)
    return report


__all__ = [
    "ConformanceReport",
    "ConformanceViolation",
    "run_conformance_suite",
    "verify_cancel_honesty",
    "verify_event_ordering",
    "verify_secret_redaction",
    "verify_start_and_terminal_event",
    "verify_tool_call_pairing",
]
