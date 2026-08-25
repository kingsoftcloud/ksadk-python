"""Conformance 校验器测试（Phase 0 最小套件，plan §15）。"""

from __future__ import annotations

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
    assert any(v.rule == "cancel-honesty" for v in run_conformance_suite(events).violations) is False


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

    from ksadk.harness.conformance.contract import verify_secret_redaction
    from ksadk.harness.config import HarnessConfig
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
