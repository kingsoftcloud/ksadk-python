"""缺口 6/7：planned/projected/actual 贯穿 Trace + native compaction 展示规范（方案 §6.3 / §2.7）。"""

from __future__ import annotations

from typing import Any

from ksadk.conversations.runtime_observability import _set_context_plan_attributes


class _FakeSpan:
    def __init__(self):
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key, value):
        self.attrs[key] = value


def test_trace_records_planned_projected_actual():
    """缺口 6：span 同时记录 planned/projected/runtime_reported。"""
    span = _FakeSpan()
    plan = {
        "plan_id": "p1",
        "planned_input_tokens": 12000,
        "projected_input_tokens": 11800,
        "runtime_reported_input_tokens": 11900,
        "integration_mode": "ksadk_hosted",
        "accounting_accuracy": "exact",
        "tokens_by_kind": {"compiled_prompt": 1800},
    }
    _set_context_plan_attributes(span, plan)
    assert span.attrs["context.planned_input_tokens"] == 12000
    assert span.attrs["context.projected_input_tokens"] == 11800
    assert span.attrs["context.runtime_reported_input_tokens"] == 11900


def test_trace_records_null_projected_when_unknown():
    """projected/runtime_reported 为 None 时不写入 span（_set_span_attribute 跳过 None，诚实标注=不伪造值）。"""
    span = _FakeSpan()
    plan = {
        "plan_id": "p1",
        "planned_input_tokens": 100,
        "projected_input_tokens": None,
        "runtime_reported_input_tokens": None,
        "accounting_accuracy": "estimated",
    }
    _set_context_plan_attributes(span, plan)
    # None 值不进 span（不伪造）；planned 仍记录
    assert span.attrs.get("context.planned_input_tokens") == 100
    assert "context.projected_input_tokens" not in span.attrs  # 未可得，不写
    assert "context.runtime_reported_input_tokens" not in span.attrs


def test_native_compaction_visibility_opaque():
    """缺口 7：native compaction 不可见时标 compaction_visibility=opaque + note。"""
    span = _FakeSpan()
    plan = {
        "compaction_owner": "native",
        "accounting_accuracy": "opaque",
        "integration_mode": "native_runtime",
    }
    _set_context_plan_attributes(span, plan)
    assert span.attrs.get("context.compaction_visibility") == "opaque"
    assert "native" in span.attrs.get("context.compaction_note", "")


def test_ksadk_compaction_no_opaque_marker():
    """ksadk compaction 不标 opaque（可见，有 exact/projected）。"""
    span = _FakeSpan()
    plan = {
        "compaction_owner": "ksadk",
        "accounting_accuracy": "exact",
        "integration_mode": "ksadk_hosted",
    }
    _set_context_plan_attributes(span, plan)
    assert "context.compaction_visibility" not in span.attrs


def test_framework_compaction_no_opaque_marker():
    """framework compaction（assisted）不标 opaque（有 projected）。"""
    span = _FakeSpan()
    plan = {
        "compaction_owner": "framework",
        "accounting_accuracy": "estimated",
        "integration_mode": "framework_assisted",
    }
    _set_context_plan_attributes(span, plan)
    assert "context.compaction_visibility" not in span.attrs


def test_empty_plan_no_crash():
    span = _FakeSpan()
    _set_context_plan_attributes(span, None)
    assert span.attrs == {}
    _set_context_plan_attributes(span, {})
    assert span.attrs.get("context.plan_id") is None
