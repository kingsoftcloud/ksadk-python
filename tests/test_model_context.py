from ksadk.conversations.model_context import (
    get_context_window_tokens,
    get_effective_context_window_tokens,
)


def test_context_window_helpers_keep_catalog_window_separate_from_effective_budget():
    metadata = {
        "id": "deepseek-v4-pro",
        "context_window_tokens": 1_024_000,
        "max_output_tokens": 384_000,
        "limits": {
            "context_window_tokens": 1_024_000,
            "max_output_tokens": 384_000,
        },
    }

    assert get_context_window_tokens(metadata) == 1_024_000
    assert get_effective_context_window_tokens(metadata) == 1_004_000
