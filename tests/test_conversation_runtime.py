from __future__ import annotations

import importlib

import pytest

from ksadk.conversations.context import build_history_from_events
from ksadk.conversations.model_context import estimate_text_tokens
from ksadk.conversations.runtime import (
    append_context_checkpoint_event,
    build_run_input,
    compact_conversation_history,
    invoke_conversation_once,
    preview_auto_compaction,
)
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


class _StubRunner:
    def __init__(self):
        self.detection_result = type("Detection", (), {"name": "demo-agent"})()
        self.calls: list[dict] = []
        self.prepared_models: list[str | None] = []

    def prepare_for_request(self, model):
        self.prepared_models.append(model)

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "assistant says hi"}


class _PromptTooLongRunner(_StubRunner):
    def __init__(self):
        super().__init__()
        self.invocation_count = 0

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        self.invocation_count += 1
        if self.invocation_count == 1:
            raise RuntimeError("prompt-too-long")
        return {"output": "compacted answer"}


def test_estimate_text_tokens_is_less_optimistic_for_cjk():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello world") == 3
    assert estimate_text_tokens("你好世界") == 4
    assert estimate_text_tokens("Agent平台设计") == 6


@pytest.mark.asyncio
async def test_build_run_input_projects_history_from_append_only_events(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "hi"}]},
        ),
    )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        messages=[{"role": "user", "content": "follow up"}],
    )

    assert prepared.history == [
        {"role": "user", "content": "hello"},
        {"role": "model", "content": "hi"},
        {"role": "user", "content": "follow up"},
    ]

    events = await service.get_events("sess-1")
    assert [event.event_type for event in events] == [
        "user_message",
        "assistant_message",
        "user_message",
    ]


@pytest.mark.asyncio
async def test_invoke_conversation_once_persists_canonical_turn_events(monkeypatch):
    service = InMemorySessionService()
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    runner = _StubRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id=None,
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o",
        prepare_runner=lambda runner, model: runner.prepare_for_request(model),
    )

    assert result["output_text"] == "assistant says hi"
    assert runner.prepared_models == ["gpt-4o"]
    assert runner.calls[-1]["history"] == [{"role": "user", "content": "hello"}]

    events = await service.get_events(session_id)
    assert [event.event_type for event in events] == [
        "user_message",
        "run_status",
        "assistant_message",
        "run_status",
    ]
    assert [event.author for event in events] == ["user", "demo-agent", "demo-agent", "demo-agent"]


@pytest.mark.asyncio
async def test_append_context_checkpoint_event_records_compaction_boundary(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-1")
    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)

    event = await append_context_checkpoint_event(
        session_id="sess-1",
        author="demo-agent",
        compacted_until_seq_id=8,
        metadata={"reason": "auto-compact"},
    )

    assert event.event_type == "context_checkpoint"
    assert event.metadata["compacted_until_seq_id"] == 8
    assert event.metadata["reason"] == "auto-compact"


def test_session_event_infers_canonical_message_types():
    user_event = SessionEvent.from_dict(
        {
            "author": "user",
            "content": {"role": "user", "parts": [{"text": "hello"}]},
        }
    )
    assistant_event = SessionEvent.from_dict(
        {
            "author": "demo-agent",
            "content": {"role": "model", "parts": [{"text": "hi"}]},
        }
    )

    assert user_event.event_type == "user_message"
    assert assistant_event.event_type == "assistant_message"


def test_build_history_from_events_prefers_latest_checkpoint_and_tail():
    events = [
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
            seq_id=1,
        ),
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "hi"}]},
            seq_id=2,
        ),
        SessionEvent(
            id="evt-3",
            author="demo-agent",
            event_type="context_checkpoint",
            content={"role": "model", "parts": [{"text": "Earlier conversation summary:\nuser: hello | assistant: hi"}]},
            seq_id=3,
            metadata={"compacted_until_seq_id": 2},
        ),
        SessionEvent(
            id="evt-4",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "follow up"}]},
            seq_id=4,
        ),
    ]

    assert build_history_from_events(events) == [
        {"role": "model", "content": "Earlier conversation summary:\nuser: hello | assistant: hi"},
        {"role": "user", "content": "follow up"},
    ]


@pytest.mark.asyncio
async def test_build_run_input_auto_compacts_old_rounds_into_checkpoint(monkeypatch):
    runtime_module = importlib.import_module("ksadk.conversations.runtime")
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-compact")
    for turn in range(5):
        await service.append_event(
            "sess-compact",
            SessionEvent(
                id=f"u-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ('x' * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )
        await service.append_event(
            "sess-compact",
            SessionEvent(
                id=f"a-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ('y' * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 120)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)

    preview = await preview_auto_compaction(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5",
        session_service_provider=lambda: service,
    )
    prepared = await build_run_input(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-compact",
        messages=[{"role": "user", "content": "follow up"}],
        model="glm-5",
    )

    events = await service.get_events("sess-compact")
    assert preview.should_compact is True
    assert preview.total_estimated_tokens > 0
    assert "compaction_boundary" in [event.event_type for event in events]
    assert "context_checkpoint" in [event.event_type for event in events]
    assert prepared.compaction_triggered is True
    assert prepared.compaction_trigger == "auto"
    assert prepared.compacted_until_seq_id is not None
    assert prepared.history[0]["role"] == "model"
    assert "Earlier conversation summary:" in prepared.history[0]["content"]
    assert prepared.history[-1] == {"role": "user", "content": "follow up"}


@pytest.mark.asyncio
async def test_invoke_conversation_once_compacts_and_retries_on_prompt_too_long(monkeypatch):
    model_context_module = importlib.import_module("ksadk.conversations.model_context")
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-ptl")
    for turn in range(4):
        await service.append_event(
            "sess-ptl",
            SessionEvent(
                id=f"u-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ('x' * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )
        await service.append_event(
            "sess-ptl",
            SessionEvent(
                id=f"a-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ('y' * 80)}]},
                invocation_id=f"inv-{turn}",
            ),
        )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(model_context_module, "DEFAULT_CONTEXT_WINDOW_TOKENS", 120)
    monkeypatch.setattr(model_context_module, "DEFAULT_MAX_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_SUMMARY_RESERVE_TOKENS", 0)
    monkeypatch.setattr(model_context_module, "AUTOCOMPACT_BUFFER_TOKENS", 20)
    runner = _PromptTooLongRunner()

    session_id, result = await invoke_conversation_once(
        runner=runner,
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-ptl",
        messages=[{"role": "user", "content": "new follow up"}],
        model="gpt-4o",
        prepare_runner=lambda current, model: current.prepare_for_request(model),
    )

    assert session_id == "sess-ptl"
    assert result["output_text"] == "compacted answer"
    assert len(runner.calls) == 2
    assert len(runner.calls[1]["history"]) < len(runner.calls[0]["history"])
    assert runner.calls[1]["history"][0]["role"] == "model"
    assert "Earlier conversation summary:" in runner.calls[1]["history"][0]["content"]

    events = await service.get_events("sess-ptl")
    assert "compaction_boundary" in [event.event_type for event in events]
    assert "context_checkpoint" in [event.event_type for event in events]


@pytest.mark.asyncio
async def test_compact_conversation_history_prefers_semantic_summary_and_records_metadata(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-semantic")

    for turn in range(3):
        await service.append_event(
            "sess-semantic",
            SessionEvent(
                id=f"u-sem-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"用户问题 {turn} " + ("甲" * 40)}]},
                invocation_id=f"sem-{turn}",
            ),
        )
        await service.append_event(
            "sess-semantic",
            SessionEvent(
                id=f"a-sem-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"助手回复 {turn} " + ("乙" * 40)}]},
                invocation_id=f"sem-{turn}",
            ),
        )

    class _FakeSummaryClient:
        is_available = True

        async def summarize(self, *, model, messages, timeout_ms):
            assert model == "glm-5"
            assert timeout_ms > 0
            assert any("当前用户目标" in item["content"] for item in messages)
            return (
                "<analysis>draft</analysis><summary>当前用户目标\n- 修复语义压缩\n\n关键约束与偏好\n- 质量优先\n\n已完成进展\n- 已生成 checkpoint\n\n重要决策/代码上下文\n- 保持 append-only 事件契约\n\n未完成事项\n- 补更多回归测试\n\n下一步工作位置\n- ksadk.conversations.runtime.compact_conversation_history</summary>",
                {"prompt_tokens": 120, "completion_tokens": 48, "total_tokens": 168},
            )

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.semantic_summary.resolve_summary_model_client",
        lambda: _FakeSummaryClient(),
    )

    checkpoint = await compact_conversation_history(
        session_id="sess-semantic",
        author="demo-agent",
        invocation_id="inv-semantic",
        model="glm-5",
        force=True,
        keep_tail_groups=1,
        session_service_provider=lambda: service,
    )

    assert checkpoint is not None
    assert checkpoint.event_type == "context_checkpoint"
    assert "<analysis>" not in checkpoint.content["parts"][0]["text"]
    assert "当前用户目标" in checkpoint.content["parts"][0]["text"]
    assert checkpoint.metadata["summary_strategy"] == "semantic"
    assert checkpoint.metadata["summary_version"] == "v1"
    assert checkpoint.metadata["summary_model"] == "glm-5"
    assert checkpoint.metadata["summary_usage"]["total_tokens"] == 168


@pytest.mark.asyncio
async def test_compact_conversation_history_falls_back_to_extractive_when_semantic_summary_fails(monkeypatch):
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-fallback")

    for turn in range(3):
        await service.append_event(
            "sess-fallback",
            SessionEvent(
                id=f"u-fb-{turn}",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": f"user-{turn} " + ("x" * 60)}]},
                invocation_id=f"fb-{turn}",
            ),
        )
        await service.append_event(
            "sess-fallback",
            SessionEvent(
                id=f"a-fb-{turn}",
                author="demo-agent",
                event_type="assistant_message",
                content={"role": "model", "parts": [{"text": f"assistant-{turn} " + ("y" * 60)}]},
                invocation_id=f"fb-{turn}",
            ),
        )

    class _BrokenSummaryClient:
        is_available = True

        async def summarize(self, *, model, messages, timeout_ms):
            raise RuntimeError("summary backend down")

    monkeypatch.setattr("ksadk.conversations.runtime.resolve_session_service", lambda: service)
    monkeypatch.setattr(
        "ksadk.conversations.semantic_summary.resolve_summary_model_client",
        lambda: _BrokenSummaryClient(),
    )

    checkpoint = await compact_conversation_history(
        session_id="sess-fallback",
        author="demo-agent",
        invocation_id="inv-fallback",
        model="glm-5",
        force=True,
        keep_tail_groups=1,
        session_service_provider=lambda: service,
    )

    assert checkpoint is not None
    assert checkpoint.metadata["summary_strategy"] == "extractive"
    assert checkpoint.metadata["summary_version"] == "v1"
    assert "summary backend down" in checkpoint.metadata["fallback_reason"]
    assert "Earlier conversation summary:" in checkpoint.content["parts"][0]["text"]


def test_plan_compaction_keeps_pending_approval_group_out_of_checkpoint():
    runtime_module = importlib.import_module("ksadk.conversations.runtime")
    events = [
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "先看第一轮"}]},
            invocation_id="inv-1",
            seq_id=1,
        ),
        SessionEvent(
            id="evt-2",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "第一轮回复"}]},
            invocation_id="inv-1",
            seq_id=2,
        ),
        SessionEvent(
            id="evt-3",
            author="demo-agent",
            event_type="approval_request",
            content={"role": "model", "parts": [{"text": "请确认是否继续执行部署"}]},
            invocation_id="inv-2",
            seq_id=3,
        ),
        SessionEvent(
            id="evt-4",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "顺便记录这个当前任务"}]},
            invocation_id="inv-3",
            seq_id=4,
        ),
    ]

    plan = runtime_module._plan_compaction(
        events,
        force=True,
        keep_tail_groups=1,
    )

    assert plan.should_compact is True
    assert [[item.seq_id for item in group] for group in plan.groups_to_compact] == [[1, 2]]
    assert plan.pinned_state["pending_approvals"]
    assert "当前任务" in plan.pinned_state["current_user_goal"]
