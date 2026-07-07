"""usage 累加工具:逐字段求和(input/output/total + details 子键)。

用于 runner 本轮内多次 LLM 调用的 usage 累积。input_tokens 各 provider 均含 cache
(Gemini prompt_token_count 含 cached_content_token_count;OpenAI prompt_tokens 含
cached_tokens;Anthropic 经 langchain_anthropic 转换后 input_tokens 含 cache_read+
cache_creation),直接相加不重复;input_token_details 键名不统一(cached/cache_read/
cache_creation),逐键求和作诊断明细。
"""
from __future__ import annotations

from typing import Any

_MAIN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")
_DETAIL_FIELDS = ("input_token_details", "output_token_details")


def accumulate_usage(acc: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """把 delta 累加进 acc,返回新 dict(不改 acc)。"""
    if not delta:
        return dict(acc)
    result = dict(acc)
    for key in _MAIN_FIELDS:
        result[key] = int(result.get(key) or 0) + int(delta.get(key) or 0)
    for detail_key in _DETAIL_FIELDS:
        delta_details = delta.get(detail_key)
        if not isinstance(delta_details, dict):
            continue
        merged = dict(result.get(detail_key) or {})
        for k, v in delta_details.items():
            if v is None:
                continue
            try:
                merged[k] = int(merged.get(k) or 0) + int(v)
            except (TypeError, ValueError):
                continue
        if merged:
            result[detail_key] = merged
    return result
