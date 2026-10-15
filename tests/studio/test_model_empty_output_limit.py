"""Explicit empty-output compatibility repair must never bypass a terminal reason."""

import json

import httpx
import pytest

from ksadk.model_gateway.client import ModelGatewayClient
from tests.studio.test_model_client import AllowNetwork
from tests.studio.test_model_wire_usage import sse
from tests.studio.test_native_model_port import MODEL, NETWORK


def chat_response(_streaming):
    return httpx.Response(200, json={"choices": [{
        "message": {"role": "assistant", "content": "42"}, "finish_reason": "length",
    }]})


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "length", "incomplete", "max_tokens", "max_output_tokens", "output_limit",
    "context_overflow", "context_length_exceeded", "policy_abort", "user_abort",
    "canceled", "cancelled",
])
@pytest.mark.parametrize("retry", [False, True])
async def test_empty_non_success_stream_repairs_only_explicit_output_caps(monkeypatch, reason, retry):
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    captured = []

    def handler(incoming):
        captured.append(json.loads(incoming.content))
        if len(captured) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}, "[DONE]",
            ]))
        return chat_response(False)

    gateway = ModelGatewayClient(network_guard=AllowNetwork(), transport=httpx.MockTransport(handler))
    try:
        chunks = [chunk async for chunk in gateway.stream(
            MODEL, messages=[{"role": "user", "content": "Fixture."}],
            network_policy=NETWORK, timeout_seconds=2.5, max_output_tokens=128,
            output_budget_mode="fixed", retry_empty_stream=retry,
        )]
        repair = retry and reason in {
            "length", "incomplete", "max_tokens", "max_output_tokens", "output_limit",
        }
        assert len(captured) == (2 if repair else 1)
        assert all(body["max_tokens"] == 128 for body in captured)
        assert chunks[-1].done and chunks[-1].finish_reason == reason
        assert not chunks[-1].tool_calls
        assert "".join(chunk.text for chunk in chunks) == ("42" if repair else "")
    finally:
        await gateway.aclose()

