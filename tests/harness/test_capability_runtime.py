"""统一 CapabilityRuntime（收口 2）测试：Policy 决策 + Receipt 幂等 + 引擎闭环。"""

from __future__ import annotations

import asyncio
import json

from ksadk.harness.capabilities import RiskLevel
from ksadk.harness.capability_runtime import (
    CapabilityRuntime,
    ToolProfile,
    arguments_digest,
)
from ksadk.harness.loop import ToolCallInput, execute_tool_calls
from ksadk.harness.tool_policy import ToolPolicy
from ksadk.harness.tool_receipts import ToolReceiptStore


def _runtime(**kwargs) -> CapabilityRuntime:
    return CapabilityRuntime(**kwargs)


class _Resolver:
    """同步审批解析器（单测语义）。"""

    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[dict] = []

    def request(self, *, call_id, name, arguments):  # type: ignore[no-untyped-def]
        self.requests.append({"call_id": call_id, "name": name, "args": arguments})
        return self.decision


class _Executor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, name: str, arguments: dict) -> str:
        self.executed.append(name)
        return f"result:{name}"


def _pending(call_id: str = "tc-1", name: str = "budget_lookup") -> list[dict]:
    return [{"call_id": call_id, "name": name, "arguments": {"q": "x"}}]


def test_policy_deny_blocks_execution_without_approval():
    executor = _Executor()
    runtime = _runtime(
        policy=ToolPolicy(denied_prefixes=("forbidden_",)),
        profiles={"forbidden_pay": ToolProfile(name="forbidden_pay")},
    )
    out = asyncio.run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=_pending(name="forbidden_pay"),
                approval_required=frozenset(),
                approval_resolver=_Resolver(),
                tool_executor=executor,
                run_id="run-1",
                capability_runtime=runtime,
            )
        )
    )
    assert executor.executed == []
    assert any("denied" in str(m["content"]) for m in out.new_messages)


def test_high_risk_profile_requires_approval():
    executor = _Executor()
    resolver = _Resolver("approved")
    runtime = _runtime(
        profiles={
            "high_risk": ToolProfile(
                name="high_risk", risk_level=RiskLevel.HIGH, side_effect="write"
            )
        }
    )
    asyncio.run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=_pending(name="high_risk"),
                approval_required=frozenset(),
                approval_resolver=resolver,
                tool_executor=executor,
                run_id="run-2",
                capability_runtime=runtime,
            )
        )
    )
    assert resolver.requests, "高风险工具必须经审批通道"
    assert executor.executed == ["high_risk"]


def test_receipt_makes_replay_idempotent():
    executor = _Executor()
    receipts = ToolReceiptStore(":memory:")
    runtime = _runtime(receipts=receipts)
    inp = ToolCallInput(
        pending_tool_calls=_pending(),
        approval_required=frozenset(),
        approval_resolver=None,
        tool_executor=executor,
        run_id="run-3",
        capability_runtime=runtime,
    )
    first = asyncio.run(execute_tool_calls(inp))
    # 审批恢复等场景重放同一节点：Receipt 命中，不再执行副作用。
    second = asyncio.run(execute_tool_calls(inp))
    assert executor.executed == ["budget_lookup"]  # 仅执行一次
    assert second.new_messages[0]["content"] == first.new_messages[0]["content"]
    replayed = [e for e in second.events if e.event_type == "tool.call.end"]
    assert replayed and replayed[0].payload.get("replayed") is True


def test_from_tool_contracts_maps_side_effect_and_approval():
    runtime = CapabilityRuntime.from_tool_contracts(
        [
            {
                "name": "pay_invoice",
                "version": "2.0.0",
                "sideEffect": "external",
            },
            {"name": "read_report", "sideEffect": "read"},
            {"name": "always_ask", "approval": "always"},
        ],
        approval_mode="policy",
    )
    pay = runtime.decide(tenant_id="t", user_id="u", agent_id="a", tool_name="pay_invoice")
    assert pay.action == "require_approval"  # external → HIGH → 审批
    read = runtime.decide(tenant_id="t", user_id="u", agent_id="a", tool_name="read_report")
    assert read.action == "allow"
    ask = runtime.decide(tenant_id="t", user_id="u", agent_id="a", tool_name="always_ask")
    assert ask.action == "require_approval"


def test_approval_mode_never_disables_all_approval():
    runtime = CapabilityRuntime.from_tool_contracts(
        [{"name": "pay_invoice", "sideEffect": "external"}],
        approval_mode="never",
    )
    decision = runtime.decide(tenant_id="t", user_id="u", agent_id="a", tool_name="pay_invoice")
    assert decision.action == "allow"


def test_arguments_digest_is_stable():
    assert arguments_digest({"a": 1, "b": 2}) == arguments_digest({"b": 2, "a": 1})
    assert arguments_digest({"a": 1}) != arguments_digest({"a": 2})


def test_receipt_store_roundtrip(tmp_path):
    store = ToolReceiptStore(str(tmp_path / "receipts.sqlite3"))
    receipt = store.record(
        __import__("ksadk.harness.tool_receipts", fromlist=["ToolReceipt"]).ToolReceipt(
            invocation_id="run-9",
            call_id="tc-9",
            tool_name="t",
            arguments_digest=arguments_digest({}),
            decision="approved",
            status="executed",
            result_digest=json.dumps({"ok": True}),
        )
    )
    assert receipt is None  # 首次写入
    existing = store.get("run-9", "tc-9")
    assert existing is not None and existing.status == "executed"
    dup = store.record(
        __import__("ksadk.harness.tool_receipts", fromlist=["ToolReceipt"]).ToolReceipt(
            invocation_id="run-9",
            call_id="tc-9",
            tool_name="t",
            arguments_digest=arguments_digest({}),
            decision="approved",
            status="executed",
            result_digest="other",
        )
    )
    assert dup is not None and dup.result_digest == existing.result_digest
    store.close()


def test_engine_capability_runtime_closes_policy_approval_receipt_loop():
    """引擎闭环：ToolProfile(HIGH) → 策略审批 → interrupt 挂起 → resume 执行 →
    Receipt 幂等（重复执行同 call 不再触发副作用）。"""
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
    from ksadk.harness.reasoner import (
        HarnessReasoningTurn,
        HarnessToolCall,
    )
    from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
    from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest

    executed: list[str] = []

    async def pay(arguments):
        executed.append("pay")
        return "付款已执行"

    async def read(arguments):
        executed.append("read")
        return "报表数据"

    class _Reasoner:
        def __init__(self) -> None:
            self.turns = 0

        async def complete(self, **kwargs):
            self.turns += 1
            if self.turns == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="tc-pay", name="pay", arguments={}),
                        HarnessToolCall(call_id="tc-read", name="read", arguments={}),
                    )
                )
            return HarnessReasoningTurn(final_text="完成")

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="财务助手"),
    )
    runtime = CapabilityRuntime(
        receipts=ToolReceiptStore(":memory:"),
        profiles={
            "pay": ToolProfile(name="pay", risk_level=RiskLevel.HIGH, side_effect="external"),
            "read": ToolProfile(name="read"),
        },
    )
    engine = ManagedLangGraphEngine(
        reasoner=_Reasoner(),
        checkpointer=InMemorySaver(),
        tools={"pay": pay, "read": read},
        capability_runtime=runtime,
    )

    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="付款并读报表",
                user_id="u",
                session_id="s",
                agent_id="a",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-cap"},
            ),
            compiled,
        )
        first = [e async for e in engine.stream(handle)]
        assert first[-1].event_type == "run.interrupted"
        await engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id="t"),
            ResumePayload(kind="approval_decision", call_id="tc-pay", data="approved"),
        )
        second = [e async for e in engine.stream(handle)]
        return first, second

    first, second = asyncio.run(drive())
    kinds1 = [e.event_type for e in first]
    assert "approval.requested" in kinds1, "HIGH 风险工具应经策略进入审批挂起"
    kinds2 = [e.event_type for e in second]
    assert "run.resumed" in kinds2 and kinds2[-1] == "run.completed"
    assert executed == ["pay", "read"], executed
    assert not any(e.payload.get("replayed") for e in second if e.event_type == "tool.call.end")
