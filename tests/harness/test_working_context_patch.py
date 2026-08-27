"""长任务方案 P1：WorkingContextPatch 版本控制测试。"""

from __future__ import annotations

import pytest

from ksadk.harness.state import WorkingContext
from ksadk.harness.working_context import record_tool_failure, record_tool_result
from ksadk.harness.working_context_patch import (
    PatchOperation,
    WorkingContextPatch,
    WorkingContextVersionError,
    apply_patch,
)


class TestApplyPatch:
    def test_set_appends_and_version_increments(self):
        working = WorkingContext()
        patched = apply_patch(
            working,
            WorkingContextPatch(
                base_version=0,
                operations=(PatchOperation(op="set", field_name="goal", value="完成预算分析"),),
                reason="user_goal",
            ),
        )
        assert patched.goal == "完成预算分析"
        assert patched.version == 1
        assert working.version == 0  # 原实例不可变

    def test_version_conflict_raises_not_silently_overwrites(self):
        working = apply_patch(
            WorkingContext(),
            WorkingContextPatch(
                base_version=0,
                operations=(PatchOperation(op="set", field_name="goal", value="v1"),),
            ),
        )
        stale = WorkingContextPatch(
            base_version=0,  # 基于旧版本
            operations=(PatchOperation(op="set", field_name="goal", value="v2"),),
        )
        with pytest.raises(WorkingContextVersionError):
            apply_patch(working, stale)

    def test_append_remove_tuple_fields(self):
        working = WorkingContext(version=3, confirmed_constraints=("预算上限 5 万",))
        patched = apply_patch(
            working,
            WorkingContextPatch(
                base_version=3,
                operations=(
                    PatchOperation(
                        op="append", field_name="confirmed_constraints", value="必须含税费"
                    ),
                ),
            ),
        )
        assert patched.confirmed_constraints == ("预算上限 5 万", "必须含税费")
        removed = apply_patch(
            patched,
            WorkingContextPatch(
                base_version=4,
                operations=(
                    PatchOperation(
                        op="remove", field_name="confirmed_constraints", value="必须含税费"
                    ),
                ),
            ),
        )
        assert removed.confirmed_constraints == ("预算上限 5 万",)
        assert removed.version == 5

    def test_unknown_field_rejected_by_validation(self):
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            apply_patch(
                WorkingContext(),
                WorkingContextPatch(
                    base_version=0,
                    operations=(PatchOperation(op="set", field_name="nonexistent", value="x"),),
                ),
            )

    def test_patch_payload_is_auditable(self):
        patch = WorkingContextPatch(
            base_version=7,
            operations=(PatchOperation(op="set", field_name="plan", value="步骤 1"),),
            source_event_ids=("evt_1", "evt_2"),
            reason="planner",
        )
        payload = patch.to_payload()
        assert payload["base_version"] == 7
        assert payload["source_event_ids"] == ["evt_1", "evt_2"]
        assert payload["reason"] == "planner"
        assert payload["operations"][0]["field"] == "plan"


class TestDeterministicUpdatesGoThroughPatch:
    def test_tool_result_bumps_version(self):
        working = WorkingContext()
        patched = record_tool_result(
            working, name="budget_lookup", result_text="预算 ¥42,000.50，审批号 AP-1024"
        )
        assert patched.version == 1
        assert any("¥42,000.50" in f for f in patched.verified_facts)

    def test_tool_failure_bumps_version_with_reason(self):
        working = WorkingContext(version=5)
        patched = record_tool_failure(
            working, name="budget_lookup", error="timeout", source_event_id="evt_9"
        )
        assert patched.version == 6
        assert patched.recent_tool_failures[0].startswith("budget_lookup: timeout")

    def test_no_facts_no_version_bump(self):
        working = WorkingContext(version=2)
        unchanged = record_tool_result(working, name="search", result_text="无关键事实")
        assert unchanged.version == 2
