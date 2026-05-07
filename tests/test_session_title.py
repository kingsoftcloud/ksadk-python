from __future__ import annotations

import pytest

from ksadk.conversations.session_title import SessionTitleClient


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
    assert captured_payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert captured_payload["extra_body"]["max_reasoning_tokens"] == 0
