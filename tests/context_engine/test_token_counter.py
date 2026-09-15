from __future__ import annotations

from ksadk.context_engine.tokenizer import (
    HEURISTIC_TOKENIZER_NAME,
    HeuristicTokenCounter,
    get_default_token_counter,
)
from ksadk.conversations.model_context import estimate_text_tokens


def test_name_matches_contract() -> None:
    assert HeuristicTokenCounter.name == HEURISTIC_TOKENIZER_NAME == "heuristic_cjk_ascii"


def test_count_text_matches_estimate_text_tokens() -> None:
    counter = HeuristicTokenCounter()
    for text in ("", "hello world", "你好，世界", "mixed 中文和 english 123"):
        assert counter.count_text(text) == estimate_text_tokens(text)


def test_count_text_empty_returns_zero() -> None:
    assert HeuristicTokenCounter().count_text("") == 0
    assert HeuristicTokenCounter().count_text("   ") == 0


def test_count_messages_accumulates_across_roles_and_parts() -> None:
    counter = HeuristicTokenCounter()
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": [{"type": "text", "text": "收到"}]},
        "plain string turn",
    ]
    expected = (
        estimate_text_tokens("user")
        + estimate_text_tokens("你好")
        + estimate_text_tokens("assistant")
        + estimate_text_tokens("收到")
        + estimate_text_tokens("plain string turn")
    )
    assert counter.count_messages(messages) == expected


def test_count_messages_handles_object_with_string_content() -> None:
    counter = HeuristicTokenCounter()

    class _Msg:
        content = "对象风格 content"

    assert counter.count_messages([_Msg()]) == estimate_text_tokens("对象风格 content")


def test_default_counter_is_singleton() -> None:
    assert get_default_token_counter() is get_default_token_counter()
    assert isinstance(get_default_token_counter(), HeuristicTokenCounter)
