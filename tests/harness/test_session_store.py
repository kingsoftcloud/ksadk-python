"""Release Session/Transcript 持久化与 Working Context 更新测试（plan §17）。"""

from __future__ import annotations

from ksadk.harness.session_store import SqliteSessionStore
from ksadk.harness.state import HarnessState, Message, MessageRole, WorkingContext
from ksadk.harness.working_context import record_tool_failure, record_tool_result


def _messages(n: int) -> list[Message]:
    return [
        Message(
            role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
            content=f"消息 {i}",
        )
        for i in range(n)
    ]


def _state(n: int) -> HarnessState:
    return HarnessState(
        tenant_id="t1",
        user_id="u1",
        agent_id="a1",
        session_id="s1",
        messages=_messages(n),
    )


class TestTranscriptPersistence:
    def test_messages_roundtrip_isolated_by_tenant_and_session(self):
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(3)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        assert len(store.messages(tenant_id="t1", session_id="s1")) == 3
        # 租户 / 会话隔离。
        assert store.messages(tenant_id="t2", session_id="s1") == []
        assert store.messages(tenant_id="t1", session_id="s2") == []

    def test_rebuild_state_from_transcript(self):
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(5)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        state = store.rebuild_state(tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1")
        assert [m.content for m in state.messages] == [m.content for m in _messages(5)]

    def test_events_persisted_and_ordered(self):
        store = SqliteSessionStore()
        from ksadk.harness.events import EventType, RuntimeEvent

        for seq in (1, 2):
            store.append_event(
                tenant_id="t1",
                session_id="s1",
                event=RuntimeEvent.create(
                    EventType.RUN_STARTED,
                    agent_id="a1",
                    user_id="u1",
                    session_id="s1",
                    invocation_id="r1",
                    seq_id=seq,
                    payload={"status": "running"},
                ),
            )
        events = store.events(tenant_id="t1", session_id="s1")
        assert [e["event_type"] for e in events] == ["run.started", "run.started"]


class TestRecoveryTranscriptWins:
    def test_consistent_state_no_recovery_event(self):
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(4)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        state, event = store.recover(_state(4), invocation_id="r1")
        assert event is None
        assert len(state.messages) == 4

    def test_inconsistency_transcript_wins_and_records_context_recovered(self):
        """Checkpoint 与 Transcript 不一致：以 Transcript 为准并记录 context.recovered。"""
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(6)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        stale = _state(3)  # Checkpoint 落后（如 Crash 前未刷写）
        recovered, event = store.recover(stale, invocation_id="r1")
        assert len(recovered.messages) == 6, "以 Transcript 为准"
        assert event is not None
        assert event.event_type == "context.recovered"
        assert "transcript_wins" in event.payload["reason"]

    def test_recovery_keeps_runtime_fields_from_checkpoint(self):
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(2)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        stale = _state(0)
        stale.working_context = WorkingContext(goal="分析预算差异")
        recovered, _ = store.recover(stale, invocation_id="r1")
        assert recovered.working_context.goal == "分析预算差异"
        assert len(recovered.messages) == 2


class TestWorkingContextUpdates:
    def test_tool_failure_recorded_bounded(self):
        working = WorkingContext()
        for i in range(12):
            working = record_tool_failure(working, name="query_budget", error=f"timeout {i}")
        assert len(working.recent_tool_failures) == 8
        assert "timeout 11" in working.recent_tool_failures[-1]
        assert "timeout 3" not in working.recent_tool_failures[0]

    def test_tool_result_critical_facts_recorded(self):
        working = WorkingContext()
        working = record_tool_result(
            working, name="query_invoice", result_text="发票 INV-2026-0001 金额 ¥12,300"
        )
        joined = " ".join(working.verified_facts)
        assert "INV-2026-0001" in joined
        assert "¥12,300" in joined

    def test_tool_result_without_facts_unchanged(self):
        working = WorkingContext()
        updated = record_tool_result(working, name="noop", result_text="一切正常")
        assert updated.verified_facts == ()

    def test_verified_facts_deduplicated(self):
        working = WorkingContext()
        working = record_tool_result(working, name="q", result_text="审批号 AP-1024")
        working = record_tool_result(working, name="q", result_text="审批号 AP-1024")
        assert len(working.verified_facts) == 1


class TestWorkingContextRebuildFromTranscript:
    """长任务方案 MVP 第 6 条：Working Context 可从 Transcript 重建。"""

    def _append_tool_end(self, store, *, seq, name, result=None, error=None):
        from ksadk.harness.events import EventType, RuntimeEvent

        payload = {"call_id": f"call-{seq}", "name": name}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        store.append_event(
            tenant_id="t1",
            session_id="s1",
            event=RuntimeEvent.create(
                EventType.TOOL_CALL_END,
                agent_id="a1",
                user_id="u1",
                session_id="s1",
                invocation_id="r1",
                seq_id=seq,
                payload=payload,
            ),
        )

    def test_rebuild_replays_tool_events_deterministically(self):
        """重放 Transcript 工具事件 == 引擎内原始 Working Context 演进。"""
        store = SqliteSessionStore()
        self._append_tool_end(
            store, seq=10, name="query_invoice", result="发票 INV-2026-0001 金额 ¥12,300"
        )
        self._append_tool_end(store, seq=11, name="query_budget", error="timeout")
        self._append_tool_end(store, seq=12, name="query_report", result="审批号 AP-1024")

        # 引擎内相同的确定性规则演进。
        expected = WorkingContext()
        expected = record_tool_result(
            expected, name="query_invoice", result_text="发票 INV-2026-0001 金额 ¥12,300"
        )
        expected = record_tool_failure(expected, name="query_budget", error="timeout")
        expected = record_tool_result(expected, name="query_report", result_text="审批号 AP-1024")

        rebuilt = store.rebuild_working_context(tenant_id="t1", session_id="s1")
        assert rebuilt == expected, "重放结果必须与原始演进一致（含 version）"

    def test_rebuild_state_includes_working_context(self):
        store = SqliteSessionStore()
        self._append_tool_end(store, seq=1, name="q", result="审批号 AP-1024")
        state = store.rebuild_state(tenant_id="t1", user_id="u1", agent_id="a1", session_id="s1")
        assert "AP-1024" in " ".join(state.working_context.verified_facts)

    def test_recover_adopts_tool_derived_fields_keeps_goal(self):
        """Transcript 重放的工具派生字段覆盖 Checkpoint；goal 等语义字段保留。"""
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(2)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        self._append_tool_end(store, seq=5, name="q", result="审批号 AP-1024")

        stale = _state(2)
        stale.working_context = WorkingContext(goal="分析预算差异")

        recovered, event = store.recover(stale, invocation_id="r1")
        # 消息一致但 Working Context 工具派生字段落后 → 仍触发恢复。
        assert event is not None
        assert recovered.working_context.goal == "分析预算差异"
        assert "AP-1024" in " ".join(recovered.working_context.verified_facts)

    def test_recover_no_event_when_tool_fields_match(self):
        store = SqliteSessionStore()
        for seq, message in enumerate(_messages(2)):
            store.append_message(tenant_id="t1", session_id="s1", message=message, seq=seq)
        state = _state(2)
        state.working_context = record_tool_result(
            state.working_context, name="q", result_text="审批号 AP-1024"
        )
        self._append_tool_end(store, seq=5, name="q", result="审批号 AP-1024")
        recovered, event = store.recover(state, invocation_id="r1")
        assert event is None
        assert recovered.working_context == state.working_context
