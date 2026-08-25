"""Conformance 校验器测试（Phase 0 最小套件，plan §15）。"""

from __future__ import annotations

from ksadk.events import EventType
from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.conformance.fixtures import (
    broken_missing_start_events,
    broken_secret_leak_events,
    broken_unpaired_tool_events,
    canceled_events,
    compliant_no_tool_events,
    compliant_single_tool_events,
    make_event_factory,
)


def test_compliant_no_tool_stream_passes():
    make = make_event_factory()
    report = run_conformance_suite(compliant_no_tool_events(make))
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]


def test_compliant_single_tool_stream_passes():
    make = make_event_factory()
    report = run_conformance_suite(compliant_single_tool_events(make))
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]


def test_unpaired_tool_call_fails():
    make = make_event_factory()
    report = run_conformance_suite(broken_unpaired_tool_events(make))
    assert not report.ok
    assert any(v.rule == "tool-pair" for v in report.violations)


def test_missing_start_event_fails():
    make = make_event_factory()
    report = run_conformance_suite(broken_missing_start_events(make))
    assert any(v.rule == "lifecycle" for v in report.violations)


def test_secret_leak_fails():
    make = make_event_factory()
    report = run_conformance_suite(broken_secret_leak_events(make))
    assert any(v.rule == "redaction" for v in report.violations)


def test_canceled_stream_passes_with_cancel_requested():
    make = make_event_factory()
    events = canceled_events(make)
    assert run_conformance_suite(events, cancel_requested=True).ok
    assert not any(
        v.rule == "cancel-honesty" for v in run_conformance_suite(events).violations
    )


def test_cancel_requested_but_completed_fails():
    make = make_event_factory()
    events = compliant_no_tool_events(make)
    report = run_conformance_suite(events, cancel_requested=True)
    assert any(v.rule == "cancel-honesty" for v in report.violations)


def test_seq_id_regression_fails():
    make = make_event_factory()
    events = compliant_no_tool_events(make)
    events[1].seq_id = 99  # 打破单调
    report = run_conformance_suite(events)
    assert any(v.rule == "ordering" for v in report.violations)


def test_existing_native_harness_adapter_stream_is_conformant():
    """现有 HarnessRuntimeAdapter（五条路径之一）的流通过最小套件。"""
    import asyncio

    from ksadk.harness.config import HarnessConfig
    from ksadk.harness.conformance.contract import verify_secret_redaction
    from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
    from ksadk.harness.runtime import HarnessRuntimeAdapter
    from ksadk.runtime import StartRequest

    class _EchoReasoner(HarnessReasoner):
        async def complete(self, *, model, prompt, messages, tools):
            return HarnessReasoningTurn(final_text="ok", tool_calls=())

    adapter = HarnessRuntimeAdapter(
        HarnessConfig(model="m", prompt="p"),
        reasoner=_EchoReasoner(),
        workspace_root="/tmp",
    )
    request = StartRequest(
        agent_id="a", user_id="u", session_id="s", input="hi",
        runtime_type="harness",
    )

    async def collect():
        handle = await adapter.start(request)
        return [event async for event in adapter.stream(handle)]

    events = asyncio.run(collect())
    report = run_conformance_suite(events)
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]
    # 显式跑一次脱敏校验，覆盖事件 payload 含 None result 的分支
    verify_secret_redaction(events, report)
    assert report.ok


# -- 加固五类：恢复/检查点/幂等/工具失败/压缩/用量 -----------------------


def _violation_rules(report):
    return [v.rule for v in report.violations]


def test_approval_resume_flow_passes_and_out_of_order_fails():
    make = make_event_factory()
    events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "ap-1", "call_id": "tc-1", "kind": "tool"},
        ),
        make(
            EventType.RUN_INTERRUPTED,
            {"status": "awaiting_approval", "reason": "tool_approval"},
        ),
        make(
            EventType.APPROVAL_RESOLVED,
            {"approval_id": "ap-1", "call_id": "tc-1", "decision": "approved"},
        ),
        make(EventType.RUN_RESUMED, {"target": "approval", "resume_kind": "approval_decision"}),
        make(EventType.TOOL_CALL_BEGIN, {"call_id": "tc-1", "name": "t"}),
        make(EventType.TOOL_CALL_END, {"call_id": "tc-1", "name": "t", "result": "r"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert run_conformance_suite(events).ok, _violation_rules(run_conformance_suite(events))

    # resolved 无 requested → approval-flow 失败
    bad = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.RUN_INTERRUPTED,
            {"status": "awaiting_approval", "reason": "tool_approval"},
        ),
        make(
            EventType.APPROVAL_RESOLVED,
            {"approval_id": "ap-x", "call_id": "tc-1", "decision": "approved"},
        ),
        make(EventType.RUN_RESUMED, {"target": "approval", "resume_kind": "approval_decision"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    report = run_conformance_suite(bad)
    assert "approval-flow" in _violation_rules(report)

    # 重复 resolved 同一 approval_id → 幂等失败
    dup = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "ap-1", "call_id": "tc-1", "kind": "tool"},
        ),
        make(
            EventType.RUN_INTERRUPTED,
            {"status": "awaiting_approval", "reason": "tool_approval"},
        ),
        make(
            EventType.APPROVAL_RESOLVED,
            {"approval_id": "ap-1", "call_id": "tc-1", "decision": "approved"},
        ),
        make(EventType.RUN_RESUMED, {"target": "approval", "resume_kind": "approval_decision"}),
        make(
            EventType.APPROVAL_RESOLVED,
            {"approval_id": "ap-1", "call_id": "tc-1", "decision": "approved"},
        ),
        make(EventType.RUN_RESUMED, {"target": "approval", "resume_kind": "approval_decision"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "approval-idempotency" in _violation_rules(run_conformance_suite(dup))


def test_resumed_without_interrupt_fails():
    make = make_event_factory()
    events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.RUN_RESUMED, {"target": "approval", "resume_kind": "approval_decision"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "recovery" in _violation_rules(run_conformance_suite(events))


def test_checkpoint_resumed_without_created_fails():
    make = make_event_factory()
    events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.CHECKPOINT_RESUMED, {"checkpoint_id": "cp-1"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "recovery" in _violation_rules(run_conformance_suite(events))

    ok_events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.CHECKPOINT_CREATED,
            {"checkpoint_id": "cp-1", "granularity": "snapshot", "invocation_id": "run-1"},
        ),
        make(EventType.CHECKPOINT_RESUMED, {"checkpoint_id": "cp-1"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    report = run_conformance_suite(ok_events)
    assert "checkpoint" not in _violation_rules(report), report.violations


def test_tool_end_with_error_and_result_fails():
    make = make_event_factory()
    events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.TOOL_CALL_BEGIN, {"call_id": "tc-1", "name": "t"}),
        make(
            EventType.TOOL_CALL_END,
            {"call_id": "tc-1", "name": "t", "result": "r", "error": "boom"},
        ),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "tool-failure" in _violation_rules(run_conformance_suite(events))


def test_model_call_nesting_and_unclosed_fail():
    make = make_event_factory()
    nested = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.MODEL_CALL_STARTED, {"model": "m"}),
        make(EventType.MODEL_CALL_STARTED, {"model": "m"}),
        make(EventType.MODEL_CALL_COMPLETED, {"model": "m"}),
        make(EventType.MODEL_CALL_COMPLETED, {"model": "m"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "model-pair" in _violation_rules(run_conformance_suite(nested))

    unclosed = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.MODEL_CALL_STARTED, {"model": "m"}),
        make(EventType.MODEL_CALL_COMPLETED, {"model": "m"}),
        make(EventType.MODEL_CALL_FAILED, {"model": "m", "error": "x"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "model-pair" in _violation_rules(run_conformance_suite(unclosed))


def test_compaction_pairing_and_budget():
    make = make_event_factory()
    ok = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.CONTEXT_COMPACTION_STARTED, {"phase": "input", "trigger": "soft_limit"}),
        make(
            EventType.CONTEXT_COMPACTION_COMPLETED,
            {"phase": "input", "trigger": "soft_limit", "compacted_until_seq_id": 5,
             "budget_tokens": 8000},
        ),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert run_conformance_suite(ok).ok, run_conformance_suite(ok).violations

    bad_budget = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.CONTEXT_COMPACTION_STARTED, {"phase": "input", "trigger": "soft_limit"}),
        make(
            EventType.CONTEXT_COMPACTION_COMPLETED,
            {"phase": "input", "trigger": "soft_limit", "compacted_until_seq_id": 5,
             "budget_tokens": 0},
        ),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "compaction" in _violation_rules(run_conformance_suite(bad_budget))

    unclosed = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(EventType.CONTEXT_COMPACTION_STARTED, {"phase": "input", "trigger": "soft_limit"}),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "compaction" in _violation_rules(run_conformance_suite(unclosed))


def test_usage_accounting_mismatch_fails():
    make = make_event_factory()
    events = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.USAGE_REPORTED,
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 20},
        ),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert "usage" in _violation_rules(run_conformance_suite(events))

    ok = [
        make(EventType.RUN_STARTED, {"status": "running"}),
        make(
            EventType.USAGE_REPORTED,
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ),
        make(EventType.RUN_COMPLETED, {"status": "succeeded"}),
    ]
    assert run_conformance_suite(ok).ok, run_conformance_suite(ok).violations
