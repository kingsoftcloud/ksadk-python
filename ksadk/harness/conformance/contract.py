"""Harness Conformance — 事件契约校验（plan §15）。

Conformance 不要求所有 Engine 原生能力相同，而是验证：相同外部契约、
事件语义一致、Tool Call 成对、不支持的能力诚实声明。本模块是纯函数
校验器，可对任意 RuntimeAdapter 的 event 流执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.events import EventType

RuntimeEvent = Any


def _payload(event: RuntimeEvent) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return payload
    dumped = event.model_dump(mode="json", exclude_none=True)
    return {
        key: value
        for key, value in dumped.items()
        if key
        not in {
            "schema_version",
            "event_id",
            "event_type",
            "seq",
            "timestamp",
            "run_id",
            "run_seq",
            "scope_id",
            "parent_scope_id",
            "source",
        }
    }


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
    """run.started 必须是首事件；必须以恰好一个终止事件收尾。

    例外：审批挂起（run.interrupted + awaiting_approval）是合法的运行中
    挂起而非终止——流停在挂起处不算违规。
    """
    if not events:
        report.fail("lifecycle", "事件流为空")
        return
    if events[0].event_type != EventType.RUN_STARTED:
        report.fail("lifecycle", f"首事件必须是 run.started，实际 {events[0].event_type}")
    terminal = [e for e in events if e.event_type in _TERMINAL_EVENTS]
    if len(terminal) != 1:
        kinds = [e.event_type for e in terminal]
        suspended = (
            events[-1].event_type == EventType.RUN_INTERRUPTED
            and str(_payload(events[-1]).get("status") or "") == "awaiting_approval"
        )
        if not (suspended and not terminal):
            report.fail("lifecycle", f"终止事件必须恰好一个，实际 {len(terminal)}: {kinds}")
    elif events[-1].event_type not in _TERMINAL_EVENTS:
        report.fail("lifecycle", "终止事件之后不允许再出现事件")


def verify_event_ordering(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """seq_id 严格递增；同一 invocation 内信封字段一致。"""
    for prev, cur in zip(events, events[1:]):
        prev_seq = getattr(prev, "seq_id", getattr(prev, "seq", -1))
        cur_seq = getattr(cur, "seq_id", getattr(cur, "seq", -1))
        if cur_seq <= prev_seq:
            report.fail(
                "ordering",
                f"seq 必须严格递增: {prev_seq} -> {cur_seq} ({cur.event_type})",
            )
    for field_name in ("user_id", "session_id", "invocation_id"):
        values = {
            getattr(e, field_name, None)
            or getattr(getattr(e, "source", None), "metadata", {}).get(field_name)
            for e in events
        }
        if len(values) > 1:
            report.fail("ordering", f"信封字段 {field_name} 在流内不一致: {values}")
    # agent_id 允许不同（收口 6：子 Agent 事件以子 agent_id 并入同一审计流）。
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
            call_id = str(_payload(event).get("call_id", ""))
            if not call_id:
                report.fail("tool-pair", "tool.call.begin 缺少 call_id")
            elif call_id in seen_begin:
                report.fail("tool-pair", f"call_id 重复 begin: {call_id}")
            else:
                seen_begin.add(call_id)
        elif event.event_type == EventType.TOOL_CALL_END:
            call_id = str(_payload(event).get("call_id", ""))
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
        scan(_payload(event), event.event_type)


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


_MODEL_CALL_CLOSERS = (EventType.MODEL_CALL_COMPLETED, EventType.MODEL_CALL_FAILED)


def verify_model_call_pairing(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """模型调用必须闭合且闭合事件对应同一模型；不允许交叉嵌套。"""
    open_model: str | None = None
    for event in events:
        if event.event_type == EventType.MODEL_CALL_STARTED:
            if open_model is not None:
                report.fail("model-pair", "model.call.started 嵌套：上一个调用尚未闭合")
            else:
                open_model = str(_payload(event).get("model") or "")
        elif event.event_type in _MODEL_CALL_CLOSERS:
            if open_model is None:
                report.fail("model-pair", f"{event.event_type} 无对应 started")
            else:
                closed_model = str(_payload(event).get("model") or "")
                if closed_model != open_model:
                    report.fail(
                        "model-pair",
                        f"{event.event_type} 模型 {closed_model!r} "
                        f"与 started {open_model!r} 不一致",
                    )
                open_model = None
    if open_model is not None:
        report.fail("model-pair", f"model.call.started {open_model!r} 未闭合")


def verify_model_provider_policy(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """重试/降级事件必须诚实声明动作，且下一次调用与动作一致。"""

    model_events = [
        event
        for event in events
        if event.event_type
        in {
            EventType.MODEL_CALL_STARTED,
            EventType.MODEL_CALL_COMPLETED,
            EventType.MODEL_CALL_FAILED,
        }
    ]
    for index, event in enumerate(model_events):
        payload = _payload(event)
        attempt = payload.get("attempt")
        if attempt is not None and (not isinstance(attempt, int) or attempt < 1):
            report.fail("model-policy", f"模型调用 attempt 非法: {attempt!r}")
        if event.event_type != EventType.MODEL_CALL_FAILED or "action" not in payload:
            continue
        action = str(payload.get("action") or "")
        if action not in {"retry_same_model", "failover", "abort", "budget_exhausted"}:
            report.fail("model-policy", f"失败动作非法: {action!r}")
            continue
        next_started = next(
            (
                candidate
                for candidate in model_events[index + 1 :]
                if candidate.event_type == EventType.MODEL_CALL_STARTED
            ),
            None,
        )
        if action in {"abort", "budget_exhausted"}:
            if next_started is not None:
                report.fail("model-policy", f"动作 {action} 后仍发起模型调用")
            continue
        if next_started is None:
            report.fail("model-policy", f"动作 {action} 后缺少下一次模型调用")
            continue
        next_payload = _payload(next_started)
        if isinstance(attempt, int) and next_payload.get("attempt") != attempt + 1:
            report.fail("model-policy", "重试/降级 attempt 未连续递增")
        same_model = next_payload.get("model") == payload.get("model")
        if action == "retry_same_model" and not same_model:
            report.fail("model-policy", "retry_same_model 却切换了模型")
        if action == "failover" and same_model:
            report.fail("model-policy", "failover 却仍调用同一模型")


def verify_tool_failure_honesty(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """tool.call.end 带 error 时不允许同时携带 result（失败不得伪装成功）。"""
    for event in events:
        if event.event_type != EventType.TOOL_CALL_END:
            continue
        payload = _payload(event)
        has_error = bool(payload.get("error"))
        has_result = "result" in payload and payload.get("result") is not None
        if has_error and has_result:
            report.fail(
                "tool-failure",
                f"tool.call.end {payload.get('call_id')} 同时携带 error 与 result",
            )


def verify_tool_reliability_honesty(
    events: list[RuntimeEvent], report: ConformanceReport
) -> None:
    """Tool 可靠性声明必须与 Receipt/Transport 事实一致。

    旧 Runner 没有该 metadata 时保持兼容；一旦声明，就不允许把只有 Receipt 的
    外部副作用工具包装为 effectively-once，也不允许 Receipt 回放事件声称未提交。
    """
    declared: dict[str, dict[str, Any]] = {}
    valid_semantics = {"replay_safe", "effectively_once", "at_least_once", "best_effort"}
    for event in events:
        if event.event_type not in {EventType.TOOL_CALL_BEGIN, EventType.TOOL_CALL_END}:
            continue
        payload = _payload(event)
        call_id = str(payload.get("call_id") or "")
        reliability = payload.get("reliability")
        if not isinstance(reliability, dict):
            continue
        semantics = str(reliability.get("semantics") or "")
        receipt_enabled = reliability.get("receipt_enabled") is True
        transport_idempotent = reliability.get("transport_idempotent") is True
        side_effect = str(reliability.get("side_effect") or "unknown")
        if semantics not in valid_semantics:
            report.fail("tool-reliability", f"{call_id} 交付语义非法: {semantics!r}")
        if semantics == "effectively_once" and not (
            receipt_enabled and transport_idempotent
        ):
            report.fail(
                "tool-reliability",
                f"{call_id} effectively_once 缺少 Receipt 或 Transport 幂等",
            )
        if semantics == "replay_safe" and side_effect not in {"none", "read"}:
            report.fail(
                "tool-reliability",
                f"{call_id} 有副作用 {side_effect!r} 却声明 replay_safe",
            )
        prior = declared.get(call_id)
        if prior is not None and prior != reliability:
            report.fail("tool-reliability", f"{call_id} begin/end 可靠性声明不一致")
        declared[call_id] = reliability
        if payload.get("replayed") is True and payload.get("receipt_committed") is not True:
            report.fail(
                "tool-reliability",
                f"{call_id} Receipt 回放事件未声明 receipt_committed=true",
            )


def verify_approval_flow(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """approval.requested → run.interrupted(审批) → approval.resolved → run.resumed 顺序守恒。"""
    requested: set[str] = set()
    resolved: set[str] = set()
    seen_interrupt = False
    for event in events:
        if event.event_type == EventType.APPROVAL_REQUESTED:
            approval_id = str(_payload(event).get("approval_id", ""))
            if approval_id and approval_id in requested:
                report.fail("approval-idempotency", f"approval_id 重复 requested: {approval_id}")
            requested.add(approval_id)
        elif event.event_type == EventType.RUN_INTERRUPTED:
            if str(_payload(event).get("reason", "")) == "tool_approval":
                seen_interrupt = True
        elif event.event_type == EventType.APPROVAL_RESOLVED:
            approval_id = str(_payload(event).get("approval_id", ""))
            if approval_id and approval_id not in requested:
                report.fail("approval-flow", f"approval.resolved 无对应 requested: {approval_id}")
            if approval_id in resolved:
                report.fail("approval-idempotency", f"approval_id 重复 resolved: {approval_id}")
            resolved.add(approval_id)
            if not seen_interrupt:
                report.fail("approval-flow", "approval.resolved 出现在 run.interrupted 之前")
        elif event.event_type == EventType.RUN_RESUMED:
            if not seen_interrupt:
                report.fail("recovery", "run.resumed 必须出现在 run.interrupted 之后")
            seen_interrupt = False


def verify_checkpoint_honesty(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """checkpoint.created 携带 checkpoint_id 且归属本 invocation；resumed 有对应 created。"""
    created: set[str] = set()
    for event in events:
        if event.event_type == EventType.CHECKPOINT_CREATED:
            payload = _payload(event)
            checkpoint_id = str(payload.get("checkpoint_id", ""))
            if not checkpoint_id:
                report.fail("checkpoint", "checkpoint.created 缺少 checkpoint_id")
            else:
                created.add(checkpoint_id)
            invocation_id = str(payload.get("invocation_id", ""))
            envelope_invocation_id = getattr(event, "invocation_id", getattr(event, "run_id", ""))
            if invocation_id and invocation_id != envelope_invocation_id:
                report.fail(
                    "checkpoint",
                    f"checkpoint.created invocation_id 与信封不一致: {invocation_id}",
                )
        elif event.event_type == EventType.CHECKPOINT_RESUMED:
            checkpoint_id = str(_payload(event).get("checkpoint_id", ""))
            if checkpoint_id not in created:
                report.fail("recovery", f"checkpoint.resumed 无对应 created: {checkpoint_id}")


def verify_compaction_honesty(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """context.compaction.started/completed 成对；completed 携带合法预算。"""
    open_compactions = 0
    for event in events:
        if event.event_type == EventType.CONTEXT_COMPACTION_STARTED:
            open_compactions += 1
            if open_compactions > 1:
                report.fail("compaction", "context.compaction.started 嵌套未闭合")
        elif event.event_type == EventType.CONTEXT_COMPACTION_COMPLETED:
            if open_compactions == 0:
                report.fail("compaction", "context.compaction.completed 无对应 started")
            else:
                open_compactions -= 1
            budget = _payload(event).get("budget_tokens")
            if not isinstance(budget, int) or budget <= 0:
                report.fail(
                    "compaction",
                    f"context.compaction.completed 预算非法: {budget!r}（须为正整数）",
                )
    if open_compactions and events and events[-1].event_type != EventType.RUN_FAILED:
        report.fail("compaction", "context.compaction.started 未被 completed 闭合")


def verify_usage_accounting(events: list[RuntimeEvent], report: ConformanceReport) -> None:
    """usage.reported 字段为非负整数且 total = input + output。"""
    for event in events:
        if event.event_type != EventType.USAGE_REPORTED:
            continue
        payload = _payload(event)
        try:
            input_tokens = int(payload.get("input_tokens") or 0)
            output_tokens = int(payload.get("output_tokens") or 0)
            total = int(payload.get("total_tokens") or 0)
        except (TypeError, ValueError):
            report.fail("usage", f"usage.reported 字段非整数: {payload}")
            continue
        if input_tokens < 0 or output_tokens < 0 or total < 0:
            report.fail("usage", f"usage.reported 出现负数: {payload}")
        elif total and total != input_tokens + output_tokens:
            report.fail(
                "usage",
                f"usage.reported total 不等于 input+output: {payload}",
            )


def verify_capability_state_transitions(
    events: list[RuntimeEvent], report: ConformanceReport
) -> None:
    """能力健康事件只能表达声明或真实状态转换，不能重复刷同一状态。

    ``declared`` 只表明 Revision 已绑定，初始状态必须是 ``unknown``，不等于
    探测成功。首个观测事件允许是 ``recovered``：能力状态可能来自上一轮 Run、持久化 Runtime
    或外部健康探针，本段事件流不一定包含它此前的 ``degraded``。一旦本流观察到
    某项能力的状态，后续相同状态事件就属于不诚实或重复上报。

    ``state`` 是增量字段，为兼容早期 RuntimeEvent v2 生产者暂不设为信封硬必填；
    生产者一旦提供，就必须与 event_type 一致。
    """
    observed: dict[str, str] = {}
    expected_states = {
        EventType.CAPABILITY_DECLARED: "unknown",
        EventType.CAPABILITY_DEGRADED: "degraded",
        EventType.CAPABILITY_RECOVERED: "available",
    }
    for event in events:
        expected = expected_states.get(event.event_type)
        if expected is None:
            continue
        payload = _payload(event)
        capability_ref = str(payload.get("capability_ref") or "")
        if not capability_ref:
            report.fail("capability-state", f"{event.event_type} 缺少 capability_ref")
            continue
        declared = payload.get("state")
        if declared is not None and str(declared) != expected:
            report.fail(
                "capability-state",
                f"{capability_ref} 的 {event.event_type} 声明了非法状态 {declared!r}",
            )
        if observed.get(capability_ref) == expected:
            report.fail(
                "capability-state",
                f"{capability_ref} 重复上报状态 {expected}",
            )
        observed[capability_ref] = expected


def run_conformance_suite(
    events: list[RuntimeEvent],
    *,
    cancel_requested: bool = False,
) -> ConformanceReport:
    """Phase 0 最小套件 + 加固五类：模型配对/工具失败/审批幂等/检查点/压缩/用量。"""
    report = ConformanceReport()
    verify_start_and_terminal_event(events, report)
    verify_event_ordering(events, report)
    verify_tool_call_pairing(events, report)
    verify_secret_redaction(events, report)
    verify_cancel_honesty(events, report, cancel_requested=cancel_requested)
    # 加固（plan §15 conformance 补齐：恢复/检查点/幂等/工具失败/压缩）。
    verify_model_call_pairing(events, report)
    verify_model_provider_policy(events, report)
    verify_tool_failure_honesty(events, report)
    verify_tool_reliability_honesty(events, report)
    verify_approval_flow(events, report)
    verify_checkpoint_honesty(events, report)
    verify_compaction_honesty(events, report)
    verify_usage_accounting(events, report)
    verify_capability_state_transitions(events, report)
    return report


__all__ = [
    "ConformanceReport",
    "ConformanceViolation",
    "run_conformance_suite",
    "verify_approval_flow",
    "verify_cancel_honesty",
    "verify_checkpoint_honesty",
    "verify_compaction_honesty",
    "verify_event_ordering",
    "verify_model_call_pairing",
    "verify_secret_redaction",
    "verify_start_and_terminal_event",
    "verify_tool_failure_honesty",
    "verify_tool_reliability_honesty",
    "verify_usage_accounting",
]
