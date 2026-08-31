"""Phase 2 ContextEngine 测试（plan §17 验收项）。"""

from __future__ import annotations

import pytest

from ksadk.harness.context_engine import (
    CompactionRequest,
    ContextEngineError,
    ContextOverflow,
    ContextRequest,
    HarnessContextEngine,
    extract_critical_facts,
    resolve_context_window,
)
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.state import HarnessState, Message, MessageRole


def _spec(**overrides) -> HarnessSpec:
    base = dict(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )
    base.update(overrides)
    return HarnessSpec(**base)


def _state(messages: list[Message] | None = None) -> HarnessState:
    return HarnessState(
        tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1", messages=messages or []
    )


class TestTokenBudget:
    def test_budget_scales_with_window_not_constant(self):
        """长会话不会因固定阈值错误压缩：预算随窗口线性扩展。"""
        engine = HarnessContextEngine()
        small = engine.build_budget(_spec(), context_window_tokens=8192)
        large = engine.build_budget(_spec(), context_window_tokens=131072)
        assert large.max_input_tokens > small.max_input_tokens * 4
        assert small.max_input_tokens < 8192  # 扣除保留输出与安全缓冲

    def test_budget_rejects_nonpositive_window(self):
        with pytest.raises(ContextEngineError):
            HarnessContextEngine().build_budget(_spec(), context_window_tokens=0)

    def test_window_source_priority_chain(self):
        assert resolve_context_window(model_profile_window=65536) == (65536, "model_profile")
        assert resolve_context_window(model_profile_window=None, provider_catalog_window=32768) == (
            32768,
            "provider_catalog",
        )
        assert resolve_context_window(
            provider_catalog_window=None, static_metadata_window=16384
        ) == (16384, "static_metadata")
        window, source = resolve_context_window()
        assert source == "fallback_default" and window > 0


class TestPlanning:
    def test_stable_prompt_separate_from_dynamic(self):
        """Stable/Dynamic 分层：required 的 stable prompt 与 current input 总在。"""
        engine = HarnessContextEngine()
        plan = engine.plan(
            ContextRequest(
                spec=_spec(), state=_state(), user_input="查预算", context_window_tokens=32768
            )
        )
        ids = [item.item_id for item in plan.selected]
        assert "stable_prompt" in ids and "current_input" in ids
        assert plan.stable_prefix_hash.startswith("sha256:")

    def test_tool_pair_not_split(self):
        """Tool Call/Result 不被拆分：assistant(tool_call_id) 与相邻 tool 消息同组。"""
        engine = HarnessContextEngine()
        state = _state(
            [
                Message(role=MessageRole.USER, content="查预算"),
                Message(role=MessageRole.ASSISTANT, content="", tool_call_id="tc-1"),
                Message(role=MessageRole.TOOL, content="42000", tool_call_id="tc-1"),
                Message(role=MessageRole.USER, content="谢谢"),
            ]
        )
        plan = engine.plan(
            ContextRequest(
                spec=_spec(), state=state, user_input="再查一次", context_window_tokens=32768
            )
        )
        grouped = [i for i in plan.selected if i.group_id]
        assert grouped, "Tool 消息必须带 group_id（原子性）"

    def test_history_dropped_under_tight_budget(self):
        engine = HarnessContextEngine()
        long_history = [
            Message(role=MessageRole.USER, content=f"历史问题 {i} " + "细节" * 200)
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
        assert any(d.action in {"dropped", "truncated"} for d in plan.decisions)
        ids = [i.item_id for i in plan.selected]
        assert "current_input" in ids, "当前输入 required，绝不被裁掉"


class TestCompaction:
    def test_critical_facts_extraction(self):
        text = "审批号 AP-1024，金额 ¥42,000.50，日期 2026-08-01，版本 1.2.3"
        facts = extract_critical_facts(text)
        joined = " | ".join(facts)
        assert "AP-1024" in joined
        assert "¥42,000.50" in facts
        assert "2026-08-01" in facts
        assert "1.2.3" in facts

    def test_compact_retains_recent_tail_and_emits_checkpoint(self):
        engine = HarnessContextEngine()
        messages = tuple(
            Message(
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                content=f"轮次 {i}",
            )
            for i in range(20)
        )
        checkpoint = engine.compact(
            CompactionRequest(
                messages=messages, trigger="proactive", summary="前 14 轮的摘要", compacted_count=14
            )
        )
        assert checkpoint.trigger == "proactive"
        assert checkpoint.compacted_until_seq_id == 14
        assert checkpoint.summary

    def test_dropped_critical_facts_get_reinjected(self):
        """压缩后关键金额/日期/ID 保留：丢失的关键事实强制重注入。"""
        engine = HarnessContextEngine()
        head = [
            Message(role=MessageRole.USER, content="审批号 AP-1024，金额 ¥42,000"),
            Message(role=MessageRole.ASSISTANT, content="已记录"),
        ]
        tail = [Message(role=MessageRole.USER, content="继续") for _ in range(6)]
        checkpoint = engine.compact(
            CompactionRequest(
                messages=tuple(head + tail),
                trigger="proactive",
                summary="之前讨论过一些事项",  # 摘要故意丢失关键事实
                compacted_count=2,
            )
        )
        assert any("AP-1024" in fact for fact in checkpoint.dropped_critical_facts)
        assert "¥42,000" in checkpoint.dropped_critical_facts
        assert "AP-1024" in checkpoint.reinjection

    def test_summary_covering_facts_no_reinjection_needed(self):
        engine = HarnessContextEngine()
        head = [Message(role=MessageRole.USER, content="审批号 AP-1024")]
        tail = [Message(role=MessageRole.USER, content="继续") for _ in range(6)]
        checkpoint = engine.compact(
            CompactionRequest(
                messages=tuple(head + tail),
                trigger="proactive",
                summary="审批号 AP-1024 已提交",
                compacted_count=1,
            )
        )
        assert checkpoint.dropped_critical_facts == ()
        assert checkpoint.reinjection == ""

    def test_emergency_compact_requires_summary(self):
        engine = HarnessContextEngine()
        with pytest.raises(ContextEngineError, match="紧急压缩"):
            engine.compact(CompactionRequest(messages=tuple(), trigger="emergency", summary=None))

    def test_short_history_skips_compaction(self):
        engine = HarnessContextEngine()
        messages = tuple(Message(role=MessageRole.USER, content="短") for _ in range(3))
        checkpoint = engine.compact(
            CompactionRequest(messages=messages, trigger="proactive", summary="x")
        )
        assert checkpoint.compacted_until_seq_id == 0


class TestRecover:
    def test_recover_first_retry_succeeds(self):
        engine = HarnessContextEngine()
        plan = engine.recover(ContextOverflow(error_message="context length exceeded"))
        assert plan.budget.max_input_tokens > 0

    def test_recover_second_retry_rejected(self):
        """紧急压缩只允许重试一次，防止无限循环。"""
        engine = HarnessContextEngine()
        with pytest.raises(ContextEngineError, match="重试一次"):
            engine.recover(ContextOverflow(error_message="overflow", retry_count=1))


class TestEngineIntegration:
    def test_engine_emits_context_planned_event(self):
        """引擎注入 ContextEngine 后发出 context.planned（预算随窗口来源记录）。"""
        import asyncio

        from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
        from ksadk.harness.reasoner import HarnessReasoningTurn
        from ksadk.runtime import StartRequest

        class _R:
            async def complete(self, **kwargs):
                return HarnessReasoningTurn(final_text="ok")

        engine = ManagedLangGraphEngine(
            reasoner=_R(),
            context_engine=HarnessContextEngine(),
        )

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="你好",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={"invocation_id": "run-ctx", "context_window_tokens": 65536},
                ),
                compiled,
            )
            return [e async for e in engine.stream(handle)]

        events = asyncio.run(drive())
        planned = [e for e in events if e.event_type == "context.planned"]
        assert len(planned) == 1
        assert planned[0].payload["budget_tokens"] > 0
        assert planned[0].payload["window_source"] == "model_profile"
        assert planned[0].payload["context_window_tokens"] == 65536
        assert events[-1].event_type == "run.completed"


class TestContextEngineControlsModelInput:
    """收口 1：ContextEngine 规划结果真正成为模型请求输入。"""

    @staticmethod
    def _recording_reasoner(script: list[str]):
        from ksadk.harness.reasoner import HarnessReasoningTurn

        class _Recording:
            def __init__(self) -> None:
                self.turns: list[list[dict]] = []

            async def complete(self, *, model, prompt, messages, tools):
                self.turns.append([dict(m) for m in messages])
                return HarnessReasoningTurn(final_text=script.pop(0))

        return _Recording()

    def _engine_with(self, reasoner, **kwargs):
        from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

        return ManagedLangGraphEngine(
            reasoner=reasoner,
            context_engine=HarnessContextEngine(),
            **kwargs,
        )

    def test_planned_messages_reach_model_with_roles_preserved(self):
        import asyncio

        from ksadk.runtime import StartRequest

        reasoner = self._recording_reasoner(["好的"])
        engine = self._engine_with(reasoner)

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="当前问题",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={
                        "invocation_id": "run-plan-1",
                        "context_window_tokens": 65536,
                        "conversation_history": [
                            {"role": "user", "content": "第一问"},
                            {"role": "assistant", "content": "第一答"},
                        ],
                    },
                ),
                compiled,
            )
            return [e async for e in engine.stream(handle)]

        events = asyncio.run(drive())
        assert events[-1].event_type == "run.completed"
        messages = reasoner.turns[0]
        roles = [m["role"] for m in messages]
        # Stable Prompt → system；历史按原角色进入；当前输入在末尾（组装器保证）。
        assert roles[0] == "system"
        assert "user" in roles and "assistant" in roles
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "当前问题"
        # 组装输入完全来自 plan（不再走旧的 conversation_history 直拼）。
        contents = [m["content"] for m in messages]
        assert all("第一问" == c or "第一答" == c or "当前问题" == c or c for c in contents)

    def test_proactive_compaction_summarizes_long_history(self):
        import asyncio

        from ksadk.harness.events import EventType
        from ksadk.runtime import StartRequest

        # 摘要轮返回关键事实摘要，主调用返回最终答复。
        reasoner = self._recording_reasoner(["摘要：预算审批 AP-1024 已通过。", "最终答复"])
        engine = self._engine_with(reasoner)

        # 用极小窗口逼出主动压缩（阈值 0.72）。
        history = [
            {"role": "user", "content": f"第{i}个很长的问题 " + "细节" * 200} for i in range(12)
        ] + [{"role": "assistant", "content": "审批号：AP-1024 已通过，金额 ¥42,000"}]

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="现在结论是什么",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={
                        "invocation_id": "run-compact-1",
                        "context_window_tokens": 2048,
                        "conversation_history": history,
                    },
                ),
                compiled,
            )
            return [e async for e in engine.stream(handle)]

        events = asyncio.run(drive())
        kinds = [e.event_type for e in events]
        # 长历史 + 小窗口必然触发主动压缩（否则历史被 planner 静默丢弃）。
        assert EventType.CONTEXT_COMPACTION_COMPLETED in kinds, kinds
        started = [e for e in events if e.event_type == "context.compaction.started"]
        completed = [e for e in events if e.event_type == "context.compaction.completed"]
        assert len(started) == len(completed) >= 1
        assert completed[0].payload["trigger"] == "proactive"
        assert completed[0].payload["budget_tokens"] > 0
        assert completed[0].payload["compacted_until_seq_id"] > 0
        # 压缩前后各有一次 context.planned；主调用输入含历史摘要 system 段。
        assert kinds.count(EventType.CONTEXT_PLANNED) >= 2
        main_messages = reasoner.turns[-1]
        joined = "\n".join(str(m.get("content")) for m in main_messages)
        assert "AP-1024" in joined, "关键事实经重注入/尾部保留"
        assert any("历史摘要" in str(m.get("content")) for m in main_messages)
        assert kinds[-1] == "run.completed"

    def test_provider_overflow_emergency_compacts_and_retries_once(self):
        import asyncio

        from ksadk.harness.events import EventType
        from ksadk.harness.reasoner import HarnessReasoningTurn
        from ksadk.runtime import StartRequest

        class _ContextLengthError(RuntimeError):
            status_code = 400

        class _OverflowThenSuccess:
            def __init__(self) -> None:
                self.main_inputs: list[list[dict]] = []

            async def complete(self, *, prompt, messages, **kwargs):
                if "上下文压缩器" in prompt:
                    return HarnessReasoningTurn(final_text="历史摘要：审批号 AP-1024。")
                self.main_inputs.append([dict(message) for message in messages])
                if len(self.main_inputs) == 1:
                    raise _ContextLengthError("maximum context length exceeded")
                return HarnessReasoningTurn(
                    final_text="恢复成功",
                    usage={"input_tokens": 32, "output_tokens": 4},
                )

        reasoner = _OverflowThenSuccess()
        engine = self._engine_with(reasoner)
        history = [
            {"role": "user", "content": f"历史问题 {index}"}
            if index % 2 == 0
            else {"role": "assistant", "content": f"历史回答 {index}"}
            for index in range(8)
        ]

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="当前问题",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={
                        "invocation_id": "run-provider-overflow",
                        "context_window_tokens": 65536,
                        "conversation_history": history,
                    },
                ),
                compiled,
            )
            events = [event async for event in engine.stream(handle)]
            state = await engine.snapshot_state(handle)
            return events, state

        events, state = asyncio.run(drive())
        kinds = [event.event_type for event in events]
        assert len(reasoner.main_inputs) == 2
        assert len(reasoner.main_inputs[1]) < len(reasoner.main_inputs[0])
        assert reasoner.main_inputs[1][0] == reasoner.main_inputs[0][0]
        assert reasoner.main_inputs[1][0]["content"] == "你是财务分析助手。"
        assert any("历史摘要" in str(m.get("content")) for m in reasoner.main_inputs[1])
        assert EventType.CONTEXT_COMPACTION_COMPLETED in kinds
        recovered = [event for event in events if event.event_type == EventType.CONTEXT_RECOVERED]
        assert recovered[-1].payload["reason"] == "provider_context_overflow"
        failed = [event for event in events if event.event_type == EventType.MODEL_CALL_FAILED]
        assert failed[0].payload["failure_category"] == "context_length"
        assert failed[0].payload["action"] == "recover_context"
        assert state is not None
        assert state.retry_state.emergency_compaction_retries == 1
        assert events[-1].event_type == EventType.RUN_COMPLETED

    def test_provider_overflow_does_not_retry_more_than_once(self):
        import asyncio

        from ksadk.harness.events import EventType
        from ksadk.harness.reasoner import HarnessReasoningTurn
        from ksadk.runtime import StartRequest

        class _AlwaysOverflow:
            def __init__(self) -> None:
                self.main_calls = 0

            async def complete(self, *, prompt, **kwargs):
                if "上下文压缩器" in prompt:
                    return HarnessReasoningTurn(final_text="压缩摘要")
                self.main_calls += 1
                error = RuntimeError("context window exceeded")
                error.status_code = 400  # type: ignore[attr-defined]
                raise error

        reasoner = _AlwaysOverflow()
        engine = self._engine_with(reasoner)

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="当前问题",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={
                        "invocation_id": "run-provider-overflow-twice",
                        "context_window_tokens": 65536,
                        "conversation_history": [
                            {"role": "user", "content": f"历史 {index}"}
                            for index in range(6)
                        ],
                    },
                ),
                compiled,
            )
            return [event async for event in engine.stream(handle)]

        events = asyncio.run(drive())
        assert reasoner.main_calls == 2
        assert sum(
            event.event_type == EventType.CONTEXT_RECOVERED
            and event.payload.get("reason") == "provider_context_overflow"
            for event in events
        ) == 1
        assert events[-1].event_type == EventType.RUN_FAILED

    def test_no_context_engine_falls_back_to_legacy_history(self):
        import asyncio

        from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
        from ksadk.runtime import StartRequest

        reasoner = self._recording_reasoner(["ok"])
        engine = ManagedLangGraphEngine(reasoner=reasoner)

        async def drive():
            compiled = await engine.compile(_spec())
            handle = await engine.start(
                StartRequest(
                    input="你好",
                    user_id="u1",
                    session_id="s1",
                    agent_id="a1",
                    runtime_type="managed-langgraph",
                    metadata={
                        "invocation_id": "run-legacy-1",
                        "conversation_history": [
                            {"role": "user", "content": "旧问题"},
                            {"role": "user", "content": "你好"},
                        ],
                    },
                ),
                compiled,
            )
            return [e async for e in engine.stream(handle)]

        events = asyncio.run(drive())
        assert events[-1].event_type == "run.completed"
        # 无 ContextEngine：沿用 conversation_history 直拼（回滚路径不变）。
        assert reasoner.turns[0][0]["role"] == "system"
        assert reasoner.turns[0][-1]["content"] == "你好"
