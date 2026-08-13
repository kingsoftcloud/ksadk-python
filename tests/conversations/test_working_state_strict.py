"""P0 严格：Working State 完整链路验收（不可放宽）。

部署 Case 长会话 → 真实 Compaction → Checkpoint → 重组 WorkingState →
严格断言四项完整（无 or []、无 if、无 or True）。
"""

from __future__ import annotations

import pytest

from ksadk.conversations.runtime_compaction import (
    _working_state_from_checkpoint,
    compact_conversation_history,
)
from ksadk.conversations.semantic_summary import WorkingState
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


def _user(seq, text, inv="inv1"):
    return SessionEvent(
        id=f"u{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=inv,
        content={"role": "user", "parts": [{"text": text}]},
    )


def _assistant(seq, text, inv="inv1"):
    return SessionEvent(
        id=f"a{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="assistant",
        invocation_id=inv,
        content={"role": "assistant", "parts": [{"text": text}]},
    )


def _deployment_events():
    """部署 Case：目标=部署服务，约束=不得操作生产，已完成=镜像已构建，下一步=dry-run。"""
    return [
        _user(1, "帮我部署这个服务到预发环境，但记住：不得操作生产环境。"),
        _assistant(2, "好的，目标是部署服务到预发。约束：不得操作生产环境。我先构建镜像。"),
        _user(3, "镜像构建好了吗？"),
        _assistant(4, "镜像已构建完成。下一步：执行预发 dry-run 部署。"),
        _user(5, "继续处理。" * 50000, inv="pre-2"),
        _assistant(6, "正在处理。" * 50000, inv="pre-2"),
        _user(7, "继续部署。" * 50000, inv="pre-3"),
        _assistant(8, "处理中。" * 50000, inv="pre-3"),
        _user(9, "继续。" * 50000, inv="pre-4"),
        _assistant(10, "处理。" * 50000, inv="pre-4"),
        _user(11, "继续。" * 50000, inv="pre-5"),
        _assistant(12, "处理。" * 50000, inv="pre-5"),
    ]


_MODEL = {"context_window_tokens": 200_000, "limits": {"max_output_tokens": 32_000}}

_SUMMARY = (
    "当前用户目标：部署服务到预发环境\n"
    "关键约束：不得操作生产环境\n"
    "已完成进展：镜像已构建\n"
    "下一步工作位置：执行预发 dry-run 部署"
)


@pytest.mark.asyncio
async def test_deployment_full_chain_strict_acceptance():
    """完整链路：Session Events → Compaction → Checkpoint → 重组 → 严格断言四项。

    P0 不可放宽：无 or []、无 if、无 or True。
    """
    service = InMemorySessionService()
    await service.create_session(agent_id="agent", user_id="user-1", session_id="sess-strict")
    for ev in _deployment_events():
        await service.append_event("sess-strict", ev)

    # 1. 真实触发 compaction
    checkpoint = await compact_conversation_history(
        session_id="sess-strict",
        author="agent",
        model="m",
        model_metadata=_MODEL,
        session_service_provider=lambda: service,
        prompt_integration_mode="ksadk_hosted",
        compaction_owner="ksadk",
    )
    assert checkpoint is not None, "应触发 compaction"

    # 2. checkpoint 含 working_state
    meta = checkpoint.metadata
    assert "working_state" in meta, "checkpoint 应含 working_state"
    ws_audit = meta["working_state"]

    # 3. checkpoint 保存了具体 completed_steps（不只 count）
    assert "completed_steps" in ws_audit, "应保存具体 completed_steps"
    assert isinstance(ws_audit["completed_steps"], list)

    # 4. 从 checkpoint 重组 WorkingState
    ws = _working_state_from_checkpoint(checkpoint)
    assert ws is not None

    # 5. P0 严格断言四项完整（不可放宽）
    assert ws.current_goal, "current_goal 丢失"
    assert "不得操作生产环境" in ws.constraints, "constraints 丢失"
    assert any("镜像已构建" in s for s in ws.completed_steps), "completed_steps 丢失"
    assert ws.next_action, "next_action 丢失"
    assert "dry-run" in ws.next_action, f"next_action 不含 dry-run: {ws.next_action!r}"
    assert ws.critical_fields_present(), "critical_fields_present 未通过"


@pytest.mark.asyncio
async def test_deployment_bad_case_merge_recovers_all_four():
    """Bad Case：压缩后摘要丢失全部四项，合并旧 checkpoint 恢复。"""
    service = InMemorySessionService()
    await service.create_session(agent_id="agent", user_id="user-1", session_id="sess-bad")
    for ev in _deployment_events():
        await service.append_event("sess-bad", ev)

    checkpoint = await compact_conversation_history(
        session_id="sess-bad",
        author="agent",
        model="m",
        model_metadata=_MODEL,
        session_service_provider=lambda: service,
        prompt_integration_mode="ksadk_hosted",
        compaction_owner="ksadk",
    )
    previous = _working_state_from_checkpoint(checkpoint)

    # 模拟新 turn 摘要丢失全部四项
    new_ws = WorkingState(current_goal="", constraints=[], completed_steps=[], next_action=None)
    merged = new_ws.merge_missing_from(previous)

    # 严格断言合并后四项完整
    assert merged.current_goal, "合并后 current_goal 仍丢失"
    assert "不得操作生产环境" in merged.constraints, "合并后 constraints 仍丢失"
    assert any("镜像已构建" in s for s in merged.completed_steps), "合并后 completed_steps 仍丢失"
    assert merged.next_action, "合并后 next_action 仍丢失"
    assert "dry-run" in merged.next_action
    assert merged.critical_fields_present(), "合并后 critical_fields_present 未通过"


@pytest.mark.asyncio
async def test_deployment_checkpoint_preserves_completed_steps_list():
    """checkpoint audit dict 保存具体 completed_steps 列表，不只 count。"""
    service = InMemorySessionService()
    await service.create_session(agent_id="agent", user_id="u", session_id="sess-list")
    for ev in _deployment_events():
        await service.append_event("sess-list", ev)
    checkpoint = await compact_conversation_history(
        session_id="sess-list",
        author="agent",
        model="m",
        model_metadata=_MODEL,
        session_service_provider=lambda: service,
        prompt_integration_mode="ksadk_hosted",
        compaction_owner="ksadk",
    )
    ws_audit = checkpoint.metadata["working_state"]
    # 具体列表存在
    assert "completed_steps" in ws_audit
    assert isinstance(ws_audit["completed_steps"], list)
    # count 也存在（兼容）
    assert "completed_steps_count" in ws_audit
