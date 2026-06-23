from __future__ import annotations

import pytest

from ksadk.conversations.session_title import (
    SessionTitleClient,
    build_heuristic_title,
    build_session_title_messages,
)


@pytest.mark.asyncio
async def test_session_title_client_disables_thinking_for_fast_title_generation(monkeypatch):
    captured_payload: dict = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "能力介绍"}}],
                "usage": {"total_tokens": 8},
            }

    class _AsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            captured_payload.update(json)
            return _Response()

    monkeypatch.setattr("ksadk.conversations.session_title.httpx.AsyncClient", _AsyncClient)

    client = SessionTitleClient(api_base="https://models.example/v1", api_key="sk-test")
    title, usage = await client.generate_title(
        model="glm-5.1",
        messages=[{"role": "user", "content": "你好"}],
        timeout_ms=1000,
    )

    assert title == "能力介绍"
    assert usage == {"total_tokens": 8}
    assert captured_payload["stream"] is False
    assert captured_payload["temperature"] == 0
    assert "reasoning_effort" not in captured_payload
    assert captured_payload["extra_body"]["max_reasoning_tokens"] == 0
    assert "thinking" not in captured_payload["extra_body"]


def test_session_title_helpers_strip_inline_think_markup():
    title = build_heuristic_title(
        first_prompt="你好，请介绍一下你自己",
        assistant_text="<think>先判断身份。</think>我是招聘助手，可以筛选简历。",
    )
    messages = build_session_title_messages(
        first_prompt="你好，请介绍一下你自己",
        assistant_text="<think>先判断身份。</think>我是招聘助手，可以筛选简历。",
    )

    assert title == "招聘助手能力"
    assert "<think" not in messages[-1]["content"]


def test_session_title_helpers_replace_file_markup_without_regex_backtracking():
    title = build_heuristic_title(
        first_prompt="[[[[[[[[[[附件]]]]]]]]]] 请分析一下",
        assistant_text="",
    )

    assert title == "附件分析"
