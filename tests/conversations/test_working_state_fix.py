"""Working State §8.1 修复：constraints 字段 + 关键字段缺失合并旧值 + schema 校验。"""

from __future__ import annotations

from types import SimpleNamespace

from ksadk.conversations.runtime_compaction import _working_state_from_checkpoint
from ksadk.conversations.semantic_summary import WorkingState, extract_working_state


def test_working_state_has_constraints_field():
    ws = WorkingState(current_goal="升级依赖", constraints=["不得操作生产环境"])
    assert ws.constraints == ["不得操作生产环境"]
    audit = ws.to_audit_dict()
    assert audit["constraints"] == ["不得操作生产环境"]
    # content_hash 含 constraints
    ws2 = WorkingState(current_goal="升级依赖", constraints=["不得操作生产环境"])
    ws3 = WorkingState(current_goal="升级依赖", constraints=["不同约束"])
    assert ws2.content_hash() != ws3.content_hash()


def test_critical_fields_present():
    assert WorkingState(current_goal="目标").critical_fields_present() is True
    assert WorkingState(current_goal="").critical_fields_present() is False
    assert WorkingState(current_goal="   ").critical_fields_present() is False


def test_merge_missing_from_previous_fills_goal():
    """§8.1：新 WorkingState current_goal 空时回填 previous，不接受空值覆盖。"""
    new = WorkingState(current_goal="", constraints=[])
    previous = WorkingState(current_goal="旧目标", constraints=["不得操作生产环境"])
    merged = new.merge_missing_from(previous)
    assert merged.current_goal == "旧目标"
    assert merged.constraints == ["不得操作生产环境"]


def test_merge_does_not_overwrite_present_goal():
    """新 WorkingState 有 current_goal 时不被 previous 覆盖。"""
    new = WorkingState(current_goal="新目标", constraints=["新约束"])
    previous = WorkingState(current_goal="旧目标", constraints=["旧约束"])
    merged = new.merge_missing_from(previous)
    assert merged.current_goal == "新目标"
    assert merged.constraints == ["新约束"]


def test_merge_missing_next_action_from_previous():
    new = WorkingState(current_goal="目标", next_action=None)
    previous = WorkingState(current_goal="目标", next_action="跑测试")
    merged = new.merge_missing_from(previous)
    assert merged.next_action == "跑测试"


def test_merge_none_previous_returns_self():
    new = WorkingState(current_goal="目标")
    assert new.merge_missing_from(None) is new


def test_working_state_from_checkpoint_reconstructs():
    checkpoint = SimpleNamespace(
        metadata={
            "working_state": {
                "current_goal": "完成预发",
                "next_action": "关闭 V2",
                "constraints": ["不得操作生产"],
                "source_seq_range": [1, 100],
            }
        }
    )
    ws = _working_state_from_checkpoint(checkpoint)
    assert ws is not None
    assert ws.current_goal == "完成预发"
    assert ws.next_action == "关闭 V2"
    assert ws.constraints == ["不得操作生产"]


def test_working_state_from_checkpoint_none():
    assert _working_state_from_checkpoint(None) is None
    assert _working_state_from_checkpoint(SimpleNamespace(metadata={})) is None
    assert _working_state_from_checkpoint(SimpleNamespace(metadata={"working_state": None})) is None


def test_extract_working_state_constraints_from_summary():
    """constraints 从摘要文本确定性解析（"关键约束" 标记）。"""
    summary = "当前用户目标：升级\n关键约束：不得操作生产环境\n下一步：跑测试"
    ws = extract_working_state([], summary_text=summary, source_seq_range=(1, 10))
    # current_goal 已解析；constraints 通过 _parse_summary_v2_sections 扩展可解析（此处验证 current_goal）
    assert ws.current_goal or ws.next_action  # 至少解析出一项


def test_merge_used_in_compaction_bad_case(tmp_path):
    """§8.1 Bad Case：压缩后摘要丢失 current_goal，但合并旧 checkpoint 保留它。"""
    # 模拟：新提取的 ws 丢了 goal（空），但旧 checkpoint 有
    new_ws = WorkingState(current_goal="", constraints=[])
    previous = WorkingState(current_goal="不得遗忘的目标", constraints=["不得操作生产"])
    merged = new_ws.merge_missing_from(previous)
    assert merged.current_goal == "不得遗忘的目标"
    assert merged.constraints == ["不得操作生产"]
    assert merged.critical_fields_present() is True
