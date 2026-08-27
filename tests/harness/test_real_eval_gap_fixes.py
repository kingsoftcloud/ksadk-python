"""真实模型评测暴露的三个缺口的回归测试（离线复现）。"""

from __future__ import annotations

from types import SimpleNamespace

from ksadk.harness.compaction_record import build_compaction_record
from ksadk.harness.evaluation import fact_retention, usage_manifest_paired_ratio
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.memory.extraction import propose_memory_candidates


def _event(event_type: str, payload: dict, seq: int = 1) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="a",
        user_id="u",
        session_id="s",
        invocation_id="r",
        seq_id=seq,
        payload=payload,
    )


class TestReinjectionNotSilentDrop:
    """缺口 1：重注入保留的事实不算最终丢弃（constraint_retention 误报）。"""

    def test_record_passes_when_all_dropped_reinjected(self):
        record = build_compaction_record(
            run_id="r",
            trigger="proactive",
            before_tokens=100,
            after_tokens=50,
            compacted_event_range=(0, 10),
            summary="摘要",
            retained_critical_facts=("INV-2026-0001",),
            dropped_critical_facts=("¥12,300",),
            reinjected_critical_facts=("¥12,300",),
        )
        checks = record.quality_checks()
        assert checks["critical_facts_preserved"] is True
        assert record.reinjected_critical_facts == ("¥12,300",)

    def test_record_fails_when_still_dropped(self):
        record = build_compaction_record(
            run_id="r",
            trigger="proactive",
            before_tokens=100,
            after_tokens=50,
            compacted_event_range=(0, 10),
            summary="摘要",
            retained_critical_facts=(),
            dropped_critical_facts=("¥12,300", "AP-1024"),
            reinjected_critical_facts=("¥12,300",),
        )
        assert record.quality_checks()["critical_facts_preserved"] is False

    def test_fact_retention_excludes_reinjected(self):
        from ksadk.harness.evaluation import LongTaskCase

        case = LongTaskCase(
            case_id="c",
            user_input="汇总",
            history=(),
            expected_facts=("¥12,300", "AP-1024"),
            context_window_tokens=2048,
        )
        events = [
            _event(
                EventType.CONTEXT_COMPACTION_COMPLETED,
                {
                    "phase": "after",
                    "trigger": "proactive",
                    "compacted_until_seq_id": 10,
                    "dropped_critical_facts": ["¥12,300", "AP-1024"],
                    "reinjected_critical_facts": ["¥12,300", "AP-1024"],
                },
            )
        ]
        assert fact_retention(case, events) == 1.0

    def test_usage_paired_ratio_ignores_compaction_usage(self):
        events = [
            _event(
                EventType.USAGE_REPORTED,
                {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                 "purpose": "compaction"},
            ),
            _event(
                EventType.USAGE_REPORTED,
                {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55,
                 "manifest_id": "ctxm_x"},
            ),
        ]
        assert usage_manifest_paired_ratio(events) == 1.0


class TestRuleAnnotatorCoverage:
    """缺口 2：规则标注器漏检偏好纠错与工具事实（recall 0.5 → 1.0）。"""

    def _propose(self, event_type: str, author: str, text: str):
        return propose_memory_candidates(
            [SimpleNamespace(id="evt_1", seq_id=1, event_type=event_type, author=author, text=text)]
        )

    def test_general_correction_annotated(self):
        candidates = self._propose("user_message", "user", "以后报表不要用英文，请改成中文。")
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.operation == "update"
        assert candidate.reason == "explicit_user_correction"
        assert "报表" in candidate.content and "中文" in candidate.content

    def test_tool_fact_query_success_annotated(self):
        candidates = self._propose(
            "tool_result", "tool", "查询成功：供应商 VEN-3101 名称华信科技，状态已认证。"
        )
        assert len(candidates) == 1
        assert candidates[0].memory_type == "fact"
        assert candidates[0].reason == "tool_fact"

    def test_plain_question_not_annotated(self):
        assert self._propose("user_message", "user", "这个函数为什么会报空指针？") == []

    def test_no_false_correction_on_negation_without_new_value(self):
        assert self._propose("user_message", "user", "不要迟到。") == []


class TestSummaryDegradationObservable:
    """缺口 3：摘要降级不静默——发 context.recovered 事件。"""

    def test_degraded_summary_emits_context_recovered(self):
        import asyncio

        from ksadk.harness.context_engine import HarnessContextEngine
        from ksadk.harness.engine.context_pipeline import EngineContextPipeline
        from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
        from ksadk.harness.state import HarnessState, Message, MessageRole

        class _FailingReasoner:
            async def complete(self, *, model, prompt, messages, tools):
                raise RuntimeError("model endpoint down")

        class _Run:
            handle = SimpleNamespace(run_id="r1")
            state = HarnessState(
                tenant_id="t", user_id="u", agent_id="a", session_id="s", run_id="r1"
            )
            request = SimpleNamespace(input="", metadata={})
            compiled = SimpleNamespace(
                spec=HarnessSpec(
                    agent_revision_ref="agent-revision://x@1",
                    model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
                    prompt=PromptSpec(instructions="i"),
                )
            )
            events = []
            seq = 0

        def _event(run, event_type, payload):
            ev = RuntimeEvent.create(
                event_type,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                invocation_id=run.handle.run_id,
                seq_id=run.seq + 1,
                payload=payload,
            )
            run.seq = ev.seq_id
            return ev

        pipeline = EngineContextPipeline(
            context_engine=HarnessContextEngine(),
            reasoner=_FailingReasoner(),
            event_fn=_event,
        )
        head = [
            Message(role=MessageRole.USER, content=f"历史 {i} " + "细节" * 200)
            for i in range(10)
        ]
        summary = asyncio.run(pipeline._summarize(_Run(), head))

        assert summary.endswith("…[截断]")
        assert any(
            e.event_type == EventType.CONTEXT_RECOVERED
            and e.payload["reason"] == "compaction_summary_failed:degraded_truncation"
            for e in _Run.events
        ), "摘要降级必须可观测（context.recovered），不允许静默"
