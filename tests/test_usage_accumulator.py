"""usage_accumulator 单测:逐字段累加(input/output/total + details 子键)。"""
from __future__ import annotations

from ksadk.runners.usage_accumulator import accumulate_usage


def test_accumulate_usage_sums_main_fields():
    acc = {}
    acc = accumulate_usage(acc, {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
    acc = accumulate_usage(acc, {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280})
    assert acc["input_tokens"] == 300
    assert acc["output_tokens"] == 130
    assert acc["total_tokens"] == 430


def test_accumulate_usage_sums_input_token_details():
    """details 键名不统一(cached/cache_read/cache_creation),逐键求和作诊断明细。"""
    acc = {}
    acc = accumulate_usage(acc, {"input_tokens": 100, "input_token_details": {"cached": 50}})
    acc = accumulate_usage(acc, {"input_tokens": 200, "input_token_details": {"cached": 30, "cache_read": 10}})
    assert acc["input_token_details"]["cached"] == 80
    assert acc["input_token_details"]["cache_read"] == 10


def test_accumulate_usage_handles_empty_delta():
    acc = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    acc = accumulate_usage(acc, {})
    assert acc["input_tokens"] == 100  # 不变


def test_accumulate_usage_does_not_mutate_input():
    """返回新 dict,不改 acc(避免共享状态)。"""
    acc = {"input_tokens": 100}
    result = accumulate_usage(acc, {"input_tokens": 200})
    assert acc["input_tokens"] == 100  # 原始未变
    assert result["input_tokens"] == 300


def test_accumulate_usage_output_token_details():
    acc = {}
    acc = accumulate_usage(acc, {"output_tokens": 50, "output_token_details": {"reasoning": 20}})
    acc = accumulate_usage(acc, {"output_tokens": 30, "output_token_details": {"reasoning": 10}})
    assert acc["output_token_details"]["reasoning"] == 30
