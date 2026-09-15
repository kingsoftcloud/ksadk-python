"""长任务方案 P1-3：用户纠错/删除/锁定 Runtime API 合同测试。"""

from __future__ import annotations

import pytest

from ksadk.harness.memory_runtime import (
    HarnessMemoryError,
    HarnessMemoryRuntime,
    MemoryWriteRequest,
)
from ksadk.harness.spec import HarnessSpec, MemoryPolicy, ModelBinding, PromptSpec


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是助手"),
        memory_policy=MemoryPolicy(enabled=True, scopes=("session", "agent", "user", "org")),
    )


def _seed(runtime: HarnessMemoryRuntime) -> str:
    evaluation, _ = runtime.write(
        MemoryWriteRequest(
            operation="add",
            content="用户偏好：报表用英文",
            scope="user",
            scope_id="user:u1",
            source="user_explicit",
            slot_key="preference:report_language",
        ),
        _spec(),
        run_id="run_seed",
    )
    assert evaluation.decision == "commit"
    result = runtime.recall(query="报表 语言", scopes=[("user", "user:u1")])
    assert result.records
    return result.records[0].memory_id


def test_correct_supersedes_with_audit_chain():
    runtime = HarnessMemoryRuntime.local_sqlite()
    memory_id = _seed(runtime)
    updated, event = runtime.correct(
        memory_id=memory_id,
        new_content="用户偏好：报表用中文",
        reason="用户明确纠正语言偏好",
        actor="user:u1",
    )
    assert updated.content == "用户偏好：报表用中文"
    assert updated.supersedes == (memory_id,)
    assert event is not None and event.payload["operation"] == "update"
    assert event.payload["source"] == "user_correction:user:u1"
    # 旧记录 superseded：召回只剩新事实。
    result = runtime.recall(query="报表 语言", scopes=[("user", "user:u1")])
    assert [r.memory_id for r in result.records] == [updated.memory_id]
    old = runtime.get(memory_id)
    assert old is not None and old.status == "superseded"


def test_correct_noop_same_content_rejected():
    runtime = HarnessMemoryRuntime.local_sqlite()
    memory_id = _seed(runtime)
    with pytest.raises(HarnessMemoryError, match="相同"):
        runtime.correct(
            memory_id=memory_id,
            new_content="用户偏好：报表用英文",
            reason="无变化",
            actor="user:u1",
        )


def test_forget_logically_deletes_with_audit():
    runtime = HarnessMemoryRuntime.local_sqlite()
    memory_id = _seed(runtime)
    deleted, event = runtime.forget(
        memory_id=memory_id, reason="用户要求遗忘", actor="user:u1"
    )
    assert deleted
    assert event is not None and event.payload["operation"] == "delete"
    result = runtime.recall(query="报表", scopes=[("user", "user:u1")])
    assert not result.records


def test_lock_then_forget_rejected_until_unlock():
    runtime = HarnessMemoryRuntime.local_sqlite()
    memory_id = _seed(runtime)
    locked, lock_event = runtime.set_lock(
        memory_id=memory_id, locked=True, reason="防止误删", actor="user:u1"
    )
    assert locked.write_policy == "locked"
    assert lock_event is not None and lock_event.payload["operation"] == "lock"
    with pytest.raises(HarnessMemoryError, match="locked"):
        runtime.forget(memory_id=memory_id, reason="尝试删除", actor="user:u1")
    # 解锁后可删。
    unlocked, _ = runtime.set_lock(
        memory_id=memory_id, locked=False, reason="允许删除", actor="user:u1"
    )
    assert unlocked.write_policy == "auto"
    deleted, _ = runtime.forget(
        memory_id=memory_id, reason="解锁后删除", actor="user:u1"
    )
    assert deleted


def test_version_conflict_detected():
    runtime = HarnessMemoryRuntime.local_sqlite()
    memory_id = _seed(runtime)
    with pytest.raises(Exception, match="version_conflict|conflict"):
        runtime.correct(
            memory_id=memory_id,
            new_content="用户偏好：报表用中文",
            reason="并发纠错",
            actor="user:u1",
            expected_version=999,  # 基于过期版本
        )
