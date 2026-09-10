"""长任务方案 P2：压缩保真 + Memory Precision/Recall 离线指标测试。"""

from __future__ import annotations

import asyncio

from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.metrics import (
    compaction_fidelity,
    evidence_fidelity,
    memory_precision_recall,
)
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )


class _ScriptedReasoner(HarnessReasoner):
    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)

    async def complete(self, *, model, prompt, messages, tools):
        return self._turns.pop(0)


def _long_request() -> StartRequest:
    history = [
        {"role": "user", "content": f"历史问题 {i} " + "细节" * 400} for i in range(30)
    ]
    return StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="总结一下",
        runtime_type="managed-langgraph",
        metadata={"conversation_history": history, "context_window_tokens": 2048},
    )


def test_compaction_fidelity_metrics_from_event_stream():
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner(
            [
                HarnessReasoningTurn(
                    final_text="摘要：审批号 AP-1024，金额 ¥42,000.50，保留目标。"
                ),
                HarnessReasoningTurn(final_text="完成"),
            ]
        ),
        context_engine=HarnessContextEngine(),
    )

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(_long_request(), compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    metrics = compaction_fidelity(events)
    assert metrics["compaction_count"] >= 1, "长历史 + 小窗口必须发生压缩"
    assert metrics["goal_retention"] == 1.0
    assert metrics["tool_pair_integrity"] == 1.0
    # 关键事实经重注入保留 → critical_facts_preserved=True。
    assert metrics["constraint_retention"] == 1.0


def test_evidence_fidelity_retention_rate():
    original = "审批号 AP-1024，金额 ¥42,000.50，日期 2026-08-01，版本 1.2.3"
    full_summary = "审批号 AP-1024，金额 ¥42,000.50，日期 2026-08-01"
    partial_summary = "审批号 AP-1024"
    assert evidence_fidelity(full_summary, original) == 0.75
    assert evidence_fidelity(partial_summary, original) == 0.25
    assert evidence_fidelity(original, original) == 1.0


def test_memory_precision_recall_metric():
    metrics = memory_precision_recall(
        written=["用户偏好报表用中文", "错误猜测的偏好"],
        relevant=["用户偏好报表用中文"],
        recalled=["用户偏好报表用中文"],
    )
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == 1.0

    miss = memory_precision_recall(
        written=["用户偏好报表用中文"],
        relevant=["用户偏好报表用中文", "用户喜欢羽毛球"],
        recalled=["用户偏好报表用中文"],
    )
    assert miss["recall"] == 0.5


def test_metrics_empty_stream_defaults_to_one():
    metrics = compaction_fidelity([])
    assert metrics["goal_retention"] == 1.0
    assert metrics["tool_pair_integrity"] == 1.0
    assert metrics["compaction_count"] == 0.0
