"""Tool Reliability Conformance v2：四个崩溃窗口与诚实能力声明。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ksadk.harness.capabilities import RiskLevel
from ksadk.harness.capability_runtime import CapabilityRuntime, ToolProfile
from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.loop import ToolCallInput, ToolExecutionContext, execute_tool_calls
from ksadk.harness.tool_receipts import ToolReceiptStore
from ksadk.harness.tool_reliability import (
    ToolDeliverySemantics,
    classify_tool_reliability,
)


def _input(
    *,
    executor: Any,
    runtime: CapabilityRuntime,
    name: str = "external_write",
    approval_required: frozenset[str] = frozenset(),
    approval_resolver: Any = None,
) -> ToolCallInput:
    return ToolCallInput(
        pending_tool_calls=[
            {"call_id": "call-stable", "name": name, "arguments": {"value": 1}}
        ],
        approval_required=approval_required,
        approval_resolver=approval_resolver,
        tool_executor=executor,
        run_id="run-stable",
        capability_runtime=runtime,
    )


class _IdempotentExecutor:
    def __init__(self, *, fail_before_external_once: bool = False) -> None:
        self.fail_before_external_once = fail_before_external_once
        self.attempts = 0
        self.effects: dict[tuple[str, str], str] = {}

    async def execute_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> str:
        del name, arguments
        self.attempts += 1
        if self.fail_before_external_once:
            self.fail_before_external_once = False
            raise ConnectionError("crash before external call")
        key = (context.run_id, context.call_id)
        self.effects.setdefault(key, "external-effect-1")
        return self.effects[key]


class _CrashBeforeReceiptRuntime(CapabilityRuntime):
    def __init__(self) -> None:
        super().__init__(
            receipts=ToolReceiptStore(":memory:"),
            profiles={"external_write": ToolProfile("external_write", side_effect="external")},
            approval_mode="never",
        )
        self.crash_once = True

    def record_receipt(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        if kwargs.get("status") == "executed" and self.crash_once:
            self.crash_once = False
            raise RuntimeError("external success before receipt")
        return super().record_receipt(**kwargs)


class _CrashAfterReceiptRuntime(CapabilityRuntime):
    def __init__(self) -> None:
        super().__init__(
            receipts=ToolReceiptStore(":memory:"),
            profiles={"external_write": ToolProfile("external_write", side_effect="external")},
            approval_mode="never",
        )
        self.crash_once = True

    def record_receipt(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        prior = super().record_receipt(**kwargs)
        if kwargs.get("status") == "executed" and self.crash_once:
            self.crash_once = False
            raise RuntimeError("receipt committed before graph checkpoint")
        return prior


def test_reliability_classification_is_honest():
    assert (
        classify_tool_reliability(side_effect="read").semantics
        is ToolDeliverySemantics.REPLAY_SAFE
    )
    assert (
        classify_tool_reliability(side_effect="external", receipt_enabled=True).semantics
        is ToolDeliverySemantics.AT_LEAST_ONCE
    )
    assert (
        classify_tool_reliability(
            side_effect="external",
            receipt_enabled=True,
            transport_idempotent=True,
        ).semantics
        is ToolDeliverySemantics.EFFECTIVELY_ONCE
    )
    assert (
        classify_tool_reliability(side_effect="external").semantics
        is ToolDeliverySemantics.BEST_EFFORT
    )


def test_crash_before_external_call_retries_without_duplicate_effect():
    runtime = CapabilityRuntime(
        receipts=ToolReceiptStore(":memory:"),
        profiles={"external_write": ToolProfile("external_write", side_effect="external")},
        approval_mode="never",
    )
    executor = _IdempotentExecutor(fail_before_external_once=True)
    first = asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    assert first.events[-1].payload["receipt_committed"] is False
    second = asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    assert len(executor.effects) == 1
    assert executor.attempts == 2
    assert second.events[-1].payload["receipt_committed"] is True


def test_crash_after_external_before_receipt_uses_stable_call_identity():
    runtime = _CrashBeforeReceiptRuntime()
    executor = _IdempotentExecutor()
    with pytest.raises(RuntimeError, match="before receipt"):
        asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    second = asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    assert executor.attempts == 2
    assert len(executor.effects) == 1, "Transport 应按稳定 run_id+call_id 去重"
    assert second.events[-1].payload["receipt_committed"] is True


def test_crash_after_receipt_before_checkpoint_replays_receipt():
    runtime = _CrashAfterReceiptRuntime()
    executor = _IdempotentExecutor()
    with pytest.raises(RuntimeError, match="before graph checkpoint"):
        asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    second = asyncio.run(execute_tool_calls(_input(executor=executor, runtime=runtime)))
    assert executor.attempts == 1
    assert len(executor.effects) == 1
    assert second.events[-1].payload["replayed"] is True
    assert second.events[-1].payload["receipt_committed"] is True


@pytest.mark.parametrize(
    "tool_name,side_effect",
    [
        ("local_write", "write"),
        ("sandbox_execute", "external"),
        ("run_subagent", "external"),
        ("mcp_call_tool", "external"),
    ],
)
def test_crash_after_checkpoint_before_terminal_is_replay_safe_for_all_tool_paths(
    tool_name: str,
    side_effect: str,
):
    runtime = CapabilityRuntime(
        receipts=ToolReceiptStore(":memory:"),
        profiles={tool_name: ToolProfile(tool_name, side_effect=side_effect)},
        approval_mode="never",
    )
    executor = _IdempotentExecutor()
    first = asyncio.run(
        execute_tool_calls(_input(executor=executor, runtime=runtime, name=tool_name))
    )
    # 模拟节点 checkpoint 已写、Run terminal 尚未写时进程退出：恢复会重放节点。
    second = asyncio.run(
        execute_tool_calls(_input(executor=executor, runtime=runtime, name=tool_name))
    )
    assert executor.attempts == 1
    assert first.events[-1].payload["receipt_committed"] is True
    assert second.events[-1].payload["replayed"] is True


def test_denied_approval_receipt_does_not_request_again_on_replay():
    class _Denied:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, **kwargs: Any) -> str:
            del kwargs
            self.calls += 1
            return "denied"

    resolver = _Denied()
    runtime = CapabilityRuntime(
        receipts=ToolReceiptStore(":memory:"),
        profiles={
            "external_write": ToolProfile(
                "external_write",
                risk_level=RiskLevel.HIGH,
                side_effect="external",
            )
        },
    )
    inp = _input(
        executor=_IdempotentExecutor(),
        runtime=runtime,
        approval_required=frozenset({"external_write"}),
        approval_resolver=resolver,
    )
    asyncio.run(execute_tool_calls(inp))
    replay = asyncio.run(execute_tool_calls(inp))
    assert resolver.calls == 1
    assert replay.events[-1].payload["replayed"] is True
    assert "denied" in replay.new_messages[0]["content"]


def test_conformance_rejects_false_effectively_once_claim():
    events = [
        RuntimeEvent.create(
            EventType.RUN_STARTED,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=1,
            payload={"status": "running"},
        ),
        RuntimeEvent.create(
            EventType.TOOL_CALL_BEGIN,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=2,
            payload={
                "call_id": "c",
                "name": "write_external",
                "reliability": {
                    "semantics": "effectively_once",
                    "side_effect": "external",
                    "receipt_enabled": True,
                    "transport_idempotent": False,
                },
            },
        ),
        RuntimeEvent.create(
            EventType.TOOL_CALL_END,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=3,
            payload={
                "call_id": "c",
                "name": "write_external",
                "result": "ok",
                "reliability": {
                    "semantics": "effectively_once",
                    "side_effect": "external",
                    "receipt_enabled": True,
                    "transport_idempotent": False,
                },
            },
        ),
        RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            agent_id="a",
            user_id="u",
            session_id="s",
            invocation_id="r",
            seq_id=4,
            payload={"status": "completed"},
        ),
    ]
    report = run_conformance_suite(events)
    assert any(v.rule == "tool-reliability" for v in report.violations)
