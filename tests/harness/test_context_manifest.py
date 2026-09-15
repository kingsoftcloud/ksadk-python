"""长任务方案 P0 测试：ContextManifest / CompactionRecord / context.built。

覆盖（plan §10 P0 验收）：

- Planned/Projected/Actual Token 闭环（usage.reported 回填 manifest）；
- context.built 事件（manifest_id + Section Token 构成）；
- 压缩 CompactionRecord 完整字段 + quality_checks 投影到
  context.compaction.completed；
- Manifest 与敏感正文分离（只存 Hash/引用/Token）。
"""

from __future__ import annotations

import asyncio

from ksadk.harness.compaction_record import build_compaction_record
from ksadk.harness.context_engine import ContextRequest, HarnessContextEngine
from ksadk.harness.context_manifest import build_manifest
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.state import HarnessState, Message, MessageRole
from ksadk.runtime import StartRequest


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )


def _state(messages: list[Message]) -> HarnessState:
    return HarnessState(
        tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1", messages=messages
    )


class TestContextManifest:
    def test_build_manifest_from_plan(self):
        engine = HarnessContextEngine()
        plan = engine.plan(
            ContextRequest(
                spec=_spec(),
                state=_state([Message(role=MessageRole.USER, content="查预算")]),
                user_input="当前问题",
                context_window_tokens=32768,
            )
        )
        manifest = build_manifest(
            plan=plan,
            projected_tokens=1234,
            run_id="run_1",
            scope_id="agent:a1",
            model_profile_ref="model-profile://kimi-k3@1.0.0",
        )
        assert manifest.manifest_id.startswith("ctxm_")
        assert manifest.planned_tokens == plan.planned_input_tokens
        assert manifest.projected_tokens == 1234
        ids = [s.item_id for s in manifest.sections]
        assert "stable_prompt" in ids and "current_input" in ids
        # 正义：Section 只记 Hash/Token，不携带正文。
        payload = manifest.to_payload()
        assert "查预算" not in str(payload)

    def test_manifest_excludes_dropped_history_sections(self):
        engine = HarnessContextEngine()
        long_history = [
            Message(role=MessageRole.USER, content=f"历史 {i} " + "细节" * 200)
            for i in range(40)
        ]
        plan = engine.plan(
            ContextRequest(
                spec=_spec(),
                state=_state(long_history),
                user_input="当前问题",
                context_window_tokens=2048,
            )
        )
        manifest = build_manifest(
            plan=plan,
            projected_tokens=1000,
            run_id="run_2",
            scope_id="agent:a1",
            model_profile_ref="model-profile://kimi-k3@1.0.0",
        )
        dropped = [s for s in manifest.sections if not s.included]
        assert dropped, "被裁掉的 Section 必须以 included=False 留痕"

    def test_with_actual_closes_planned_projected_actual_loop(self):
        engine = HarnessContextEngine()
        plan = engine.plan(
            ContextRequest(
                spec=_spec(),
                state=_state([]),
                user_input="你好",
                context_window_tokens=32768,
            )
        )
        manifest = build_manifest(
            plan=plan,
            projected_tokens=500,
            run_id="run_3",
            scope_id="agent:a1",
            model_profile_ref="model-profile://kimi-k3@1.0.0",
        )
        assert manifest.actual_input_tokens is None
        closed = manifest.with_actual(
            input_tokens=520, output_tokens=80, usage_ref="evt_usage"
        )
        assert closed.actual_input_tokens == 520
        assert closed.actual_usage_ref == "evt_usage"
        assert closed.planned_tokens == manifest.planned_tokens  # 原 manifest 不变


class TestCompactionRecord:
    def test_quality_checks_derived_from_dropped_facts(self):
        record = build_compaction_record(
            run_id="run_4",
            trigger="proactive",
            before_tokens=92000,
            after_tokens=31000,
            compacted_event_range=(120, 480),
            summary="摘要",
            retained_critical_facts=("AP-1024",),
            dropped_critical_facts=(),
        )
        assert record.compaction_id.startswith("cmp_")
        assert record.quality_checks()["critical_facts_preserved"] is True

    def test_dropped_critical_facts_flags_quality(self):
        record = build_compaction_record(
            run_id="run_5",
            trigger="emergency",
            before_tokens=90000,
            after_tokens=30000,
            compacted_event_range=(0, 100),
            summary="摘要",
            retained_critical_facts=(),
            dropped_critical_facts=("¥42,000.50",),
        )
        assert record.quality_checks()["critical_facts_preserved"] is False
        payload = record.to_payload()
        assert payload["quality_checks"]["critical_facts_preserved"] is False


class _ScriptedReasoner(HarnessReasoner):
    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)

    async def complete(self, *, model, prompt, messages, tools):
        return self._turns.pop(0)


def _history_request() -> StartRequest:
    history = [
        {"role": "user", "content": f"历史问题 {i} " + "细节" * 400}
        for i in range(30)
    ]
    return StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="现在总结一下",
        runtime_type="managed-langgraph",
        metadata={
            "conversation_history": history,
            "context_window_tokens": 2048,
        },
    )


class TestEngineEmission:
    def test_context_built_event_and_actual_backfill(self):
        """context.built 落事件流；usage.reported 回填 manifest Actual。"""
        engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner(
                [
                    # 压缩摘要调用（_summarize 走同一 reasoner）。
                    HarnessReasoningTurn(final_text="摘要：历史问题若干。"),
                    # 最终回答，携带 usage 触发回填。
                    HarnessReasoningTurn(
                        final_text="完成", usage={"input_tokens": 640, "output_tokens": 32}
                    ),
                ]
            ),
            context_engine=HarnessContextEngine(),
        )

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(_history_request(), compiled)
            return [event async for event in engine.stream(handle)]

        events = asyncio.run(drive())
        built = [e for e in events if e.event_type == EventType.CONTEXT_BUILT]
        assert built, "必须发 context.built"
        payload = built[-1].payload
        assert payload["manifest_id"].startswith("ctxm_")
        assert payload["planned_tokens"] >= 0 and payload["projected_tokens"] >= 0
        assert payload["sections"], "Section Token 构成必须给出"
        # Actual 回填路径：usage.reported 在 context.built 之后同流落账。
        usage = [e for e in events if e.event_type == EventType.USAGE_REPORTED]
        assert usage
        assert events.index(usage[0]) > events.index(built[-1])

    def test_compaction_record_emitted_on_proactive_compaction(self):
        engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner(
                [
                    HarnessReasoningTurn(final_text="摘要：审批号 AP-1024，金额 ¥42,000。"),
                    HarnessReasoningTurn(final_text="完成"),
                ]
            ),
            context_engine=HarnessContextEngine(),
        )

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(_history_request(), compiled)
            return [event async for event in engine.stream(handle)]

        events = asyncio.run(drive())
        completed = [
            e
            for e in events
            if e.event_type == EventType.CONTEXT_COMPACTION_COMPLETED
        ]
        assert completed, "长历史 + 小窗口必须触发主动压缩"
        payload = completed[-1].payload
        assert payload["compaction_id"].startswith("cmp_")
        assert payload["before_tokens"] >= payload["after_tokens"]
        assert "quality_checks" in payload
        assert set(payload["quality_checks"]) == {
            "tool_pairs_complete",
            "approvals_preserved",
            "goal_preserved",
            "critical_facts_preserved",
        }
