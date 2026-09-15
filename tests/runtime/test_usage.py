from __future__ import annotations

import pytest

from ksadk.runtime.usage import canonical_usage_payload


@pytest.mark.parametrize(
    "raw_usage",
    [
        {
            "input_tokens": 2488,
            "output_tokens": 576,
            "total_tokens": 3064,
            "cached_tokens": 2240,
            "reasoning_tokens": 327,
        },
        {
            "input_tokens": 2488,
            "output_tokens": 576,
            "total_tokens": 3064,
            "input_token_details": {"cache_read": 2240},
            "output_token_details": {"reasoning": 327},
        },
        {
            "input_tokens": 2488,
            "output_tokens": 576,
            "total_tokens": 3064,
            "input_tokens_details": {"cached_tokens": 2240},
            "output_tokens_details": {"reasoning_tokens": 327},
        },
        {
            "prompt_tokens": 2488,
            "completion_tokens": 576,
            "total_tokens": 3064,
            "prompt_tokens_details": {"cached_tokens": 2240},
            "completion_tokens_details": {"reasoning_tokens": 327},
        },
    ],
)
def test_canonical_usage_payload_preserves_usage_detail_aliases(raw_usage):
    assert canonical_usage_payload(raw_usage, runtime_type="langgraph") == {
        "input_tokens": 2488,
        "output_tokens": 576,
        "total_tokens": 3064,
        "cached_tokens": 2240,
        "reasoning_tokens": 327,
        "source": "langgraph",
    }
