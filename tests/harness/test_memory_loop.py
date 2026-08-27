"""缺口 3：Memory 写回闭环——受控提取 → 去重/纠错 → 写回 → 审计。"""

from __future__ import annotations

from ksadk.harness.events import EventType
from ksadk.harness.memory_loop import write_back_messages
from ksadk.harness.memory_runtime import HarnessMemoryRuntime, MemoryWriteRequest
from ksadk.harness.spec import (
    HarnessSpec,
    MemoryPolicy,
    ModelBinding,
    PromptSpec,
)
from ksadk.harness.state import Message, MessageRole


def _spec(*, scopes: tuple[str, ...] = ("session", "agent")) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="助手"),
        memory_policy=MemoryPolicy(enabled=True, scopes=scopes),
    )


def _messages(*texts: str) -> list[Message]:
    return [Message(role=MessageRole.USER, content=t) for t in texts]


def test_write_back_extracts_explicit_memory_and_audits():
    runtime = HarnessMemoryRuntime.local_sqlite()
    events = write_back_messages(
        runtime,
        _spec(),
        run_id="run-1",
        agent_id="agent-1",
        user_id="user-1",
        messages=_messages("记住：我的爱好是爬山"),
    )
    assert [e.event_type for e in events] == [EventType.MEMORY_WRITE]
    assert events[0].payload["decision"] == "commit"
    # 写回后可被召回（闭环：写 → 读）。
    recalled = runtime.recall(query="爱好", scopes=[("agent", "agent:agent-1")], top_k=4)
    assert any("爬山" in r.content for r in recalled.records)


def test_write_back_dedups_identical_content():
    runtime = HarnessMemoryRuntime.local_sqlite()
    spec = _spec()
    kwargs = dict(run_id="run-2", agent_id="agent-1", user_id="user-1")
    first = write_back_messages(runtime, spec, messages=_messages("记住：我的爱好是爬山"), **kwargs)
    second = write_back_messages(
        runtime, spec, messages=_messages("记住：我的爱好是爬山"), **kwargs
    )
    assert first[0].payload["decision"] == "commit"
    assert second[0].payload["decision"] == "reject"
    assert "duplicate" in second[0].payload["reason"]
    # 只有一条 active 记录。
    recalled = runtime.recall(query="爱好", scopes=[("agent", "agent:agent-1")], top_k=10)
    active = [r for r in recalled.records if r.content == "我的爱好是爬山"]
    assert len(active) == 1


def test_write_back_supersedes_slot_conflict():
    runtime = HarnessMemoryRuntime.local_sqlite()
    spec = _spec()
    kwargs = dict(run_id="run-3", agent_id="agent-1", user_id="user-1")
    write_back_messages(runtime, spec, messages=_messages("我的爱好其实是爬山"), **kwargs)
    events = write_back_messages(
        runtime, spec, messages=_messages("我的爱好现在改成游泳了"), **kwargs
    )
    assert events and events[0].event_type == EventType.MEMORY_CONFLICT
    assert events[0].payload["decision"] == "commit"
    # 召回只看到新事实（旧事实 superseded，保留审计链）。
    recalled = runtime.recall(query="爱好", scopes=[("agent", "agent:agent-1")], top_k=10)
    contents = [r.content for r in recalled.records if r.status == "active"]
    assert contents == ["我的爱好是游泳了"]


def test_write_back_skips_when_memory_disabled():
    runtime = HarnessMemoryRuntime.local_sqlite()
    spec = _spec()
    spec = spec.model_copy(update={"memory_policy": MemoryPolicy(enabled=False)})
    events = write_back_messages(
        runtime,
        spec,
        run_id="run-4",
        agent_id="agent-1",
        user_id="user-1",
        messages=_messages("记住：我的爱好是爬山"),
    )
    assert events == []


def test_write_back_ignores_plain_chitchat():
    runtime = HarnessMemoryRuntime.local_sqlite()
    events = write_back_messages(
        runtime,
        _spec(),
        run_id="run-5",
        agent_id="agent-1",
        user_id="user-1",
        messages=_messages("你好", "今天天气不错"),
    )
    assert events == []


def test_write_back_rejected_violation_records_audit_not_raise():
    """越权写入（org scope 非 user_explicit）→ rejected 审计，不抛出。"""
    from ksadk.harness.memory_loop import _write_one

    runtime = HarnessMemoryRuntime.local_sqlite()
    request = MemoryWriteRequest(
        operation="add",
        content="组织级事实",
        scope="org",
        scope_id="org:org-1",
        source="extraction",
    )
    event = _write_one(runtime, request, _spec(), run_id="run-6", seq=0)
    assert event is not None
    assert event.event_type == EventType.MEMORY_WRITE
    assert event.payload["decision"] == "rejected"
    assert "org" in event.payload["reason"]


def test_write_back_user_scope_requires_policy_allowlist():
    runtime = HarnessMemoryRuntime.local_sqlite()
    request = MemoryWriteRequest(
        operation="add",
        content="偏好：深色模式",
        scope="user",
        scope_id="user:user-1",
        source="extraction",
    )
    import pytest

    from ksadk.harness.memory_runtime import HarnessMemoryError

    with pytest.raises(HarnessMemoryError):
        runtime.write(request, _spec(scopes=("session", "agent")), run_id="r")
