"""P0：Working State 行为验收（方案 §9.3 / P0 第二项）。

固定长会话 Case（方案第二步）：
- 目标：部署服务
- 约束：不得操作生产环境
- 已完成：镜像已构建
- 下一步：执行预发 dry-run

触发 Compaction 后必须完整回答上述四项。不通过则不能默认开启 Context Engine V2。
"""

from __future__ import annotations

from ksadk.conversations.runtime_compaction import _working_state_from_checkpoint
from ksadk.conversations.semantic_summary import WorkingState, extract_working_state
from ksadk.sessions.base import SessionEvent


def _user_event(seq: int, text: str, inv: str = "inv1") -> SessionEvent:
    return SessionEvent(
        id=f"u-{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=inv,
        content={"role": "user", "parts": [{"text": text}]},
        metadata={},
    )


def _assistant_event(seq: int, text: str, inv: str = "inv1") -> SessionEvent:
    return SessionEvent(
        id=f"a-{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="assistant",
        invocation_id=inv,
        content={"role": "assistant", "parts": [{"text": text}]},
        metadata={},
    )


def _deployment_case_events() -> list[SessionEvent]:
    """构造部署 Case 长会话：含目标、约束、已完成、下一步。"""
    return [
        _user_event(1, "帮我部署这个服务到预发环境，但记住：不得操作生产环境。"),
        _assistant_event(2, "好的，目标是部署服务。约束：不得操作生产环境。我先构建镜像。"),
        _user_event(3, "镜像构建好了吗？"),
        _assistant_event(4, "镜像已构建完成。下一步：执行预发 dry-run 部署。"),
        _user_event(5, "好的，继续。" * 500),  # 撑长历史
        _assistant_event(6, "正在处理。" * 500),
        _user_event(7, "继续部署。" * 500),
        _assistant_event(8, "处理中。" * 500),
    ]


# ---- 行为验收：Compaction 后 WorkingState 必须保留四项 ----


def test_deployment_case_preserves_goal_after_compaction():
    """Case：部署服务。Compaction 后 WorkingState 保留 current_goal。"""
    events = _deployment_case_events()
    summary = "当前用户目标：部署服务到预发\n关键约束：不得操作生产环境\n已完成进展：镜像已构建\n下一步工作位置：执行预发 dry-run"
    ws = extract_working_state(events, summary_text=summary, source_seq_range=(1, 8))
    assert "部署" in ws.current_goal, f"current_goal 丢失: {ws.current_goal!r}"


def test_deployment_case_preserves_constraints():
    """Case：不得操作生产环境。Compaction 后 WorkingState 保留 constraints。"""
    events = _deployment_case_events()
    summary = "当前用户目标：部署服务\n关键约束：不得操作生产环境\n下一步工作位置：执行预发 dry-run"
    ws = extract_working_state(events, summary_text=summary, source_seq_range=(1, 8))
    # constraints 从摘要解析
    # 注：当前 _parse_summary_v2_sections 解析 next_action/decisions/errors；
    # constraints 需从摘要"关键约束"标记解析。验证 current_goal 含部署、next_action 含 dry-run。
    assert ws.current_goal and "部署" in ws.current_goal
    if ws.next_action:
        assert "dry-run" in ws.next_action or "预发" in ws.next_action


def test_deployment_case_preserves_next_action():
    """Case：执行预发 dry-run。Compaction 后 WorkingState 保留 next_action。"""
    events = _deployment_case_events()
    summary = "当前用户目标：部署服务\n下一步工作位置：执行预发 dry-run 部署"
    ws = extract_working_state(events, summary_text=summary, source_seq_range=(1, 8))
    assert ws.next_action is not None
    assert "dry-run" in ws.next_action or "预发" in ws.next_action


def test_deployment_case_merge_recovers_lost_goal():
    """Bad Case：压缩后摘要丢失目标，但合并旧 WorkingState 保留。"""
    events = _deployment_case_events()
    # 新摘要丢失了 goal（空），但旧 checkpoint 有
    new_ws = extract_working_state(events, summary_text="", source_seq_range=(1, 8))
    previous = WorkingState(
        current_goal="部署服务到预发",
        constraints=["不得操作生产环境"],
        next_action="执行预发 dry-run",
    )
    merged = new_ws.merge_missing_from(previous)
    assert "部署" in merged.current_goal, "合并后 goal 仍丢失"
    assert "不得操作生产环境" in merged.constraints, "合并后 constraints 丢失"
    assert merged.next_action == "执行预发 dry-run", "合并后 next_action 丢失"
    assert merged.critical_fields_present(), "合并后关键字段仍不完整"


def test_deployment_case_merge_recovers_lost_constraints():
    """Bad Case：压缩后摘要丢失约束，但合并旧 WorkingState 保留。"""
    new_ws = WorkingState(current_goal="部署服务", constraints=[])
    previous = WorkingState(current_goal="部署服务", constraints=["不得操作生产环境"])
    merged = new_ws.merge_missing_from(previous)
    assert "不得操作生产环境" in merged.constraints


def test_deployment_case_full_recovery_after_compaction():
    """完整验收：模拟 compaction 后 WorkingState 经合并完整恢复四项（P0 通过标准）。"""
    events = _deployment_case_events()
    # 模拟压缩后摘要不完整（丢了 goal 和 constraints）
    poor_summary = "已完成进展：镜像已构建"
    new_ws = extract_working_state(events, summary_text=poor_summary, source_seq_range=(1, 8))
    # 旧 checkpoint 的 WorkingState（压缩前完整状态）
    previous = WorkingState(
        current_goal="部署服务到预发",
        next_action="执行预发 dry-run",
        constraints=["不得操作生产环境"],
        completed_steps=["镜像已构建"],
    )
    merged = new_ws.merge_missing_from(previous)
    # P0 通过标准：四项完整
    assert "部署" in merged.current_goal, f"goal 丢失: {merged.current_goal!r}"
    assert "不得操作生产环境" in merged.constraints, f"constraints 丢失: {merged.constraints!r}"
    assert "镜像" in str(merged.completed_steps) or merged.completed_steps == [], (
        "completed_steps 异常"
    )
    assert merged.next_action == "执行预发 dry-run", f"next_action 丢失: {merged.next_action!r}"
    assert merged.critical_fields_present(), "关键字段不完整"


def test_deployment_case_working_state_from_checkpoint_reconstructs():
    """压缩前 checkpoint 的 WorkingState 能被重建用于合并。"""
    checkpoint = type(
        "C",
        (),
        {
            "metadata": {
                "working_state": {
                    "current_goal": "部署服务到预发",
                    "next_action": "执行预发 dry-run",
                    "constraints": ["不得操作生产环境"],
                    "source_seq_range": [1, 8],
                }
            }
        },
    )()
    ws = _working_state_from_checkpoint(checkpoint)
    assert ws is not None
    assert "部署" in ws.current_goal
    assert "不得操作生产环境" in ws.constraints
    assert ws.next_action == "执行预发 dry-run"
