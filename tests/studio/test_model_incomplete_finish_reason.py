"""Observed non-success termination survives an unfilled streamed tool call."""
import json
import socket

import httpx
import pytest

from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.machine import reduce_model
from ksadk.harness.native.model import ModelProfile, normalize_response
from ksadk.harness.native.ports import RunConfiguration, RunLimits
from ksadk.harness.native.state import RunPhase, RunSnapshot
from ksadk.model_gateway.client import ModelGatewayClient
from tests.studio.test_model_client import AllowNetwork
from tests.studio.test_native_model_port import SCOPE, TOOL, port, request


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    monkeypatch.setenv("FIXTURE_KEY", "local-synthetic-value")
    monkeypatch.delenv("KSADK_MODEL_COMPAT_RULE", raising=False)

    def denied(*_args, **_kwargs):
        raise AssertionError("No network is permitted by this fixture")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def body(*, finish, arguments="", done=True, usage=True):
    chunks = [{"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call-1", "type": "function",
        "function": {"name": TOOL.name, "arguments": arguments},
    }]}, "finish_reason": finish}]}]
    if usage:
        chunks.append({"choices": [], "usage": {
            "prompt_tokens": 20, "completion_tokens": 1024,
        }})
    return b"".join(
        ("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks
    ) + (b"data: [DONE]\n\n" if done else b"")


async def result_from(raw):
    dispatched = []

    def provider(incoming):
        dispatched.append(incoming.url.host)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)

    client = ModelGatewayClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(provider)
    )
    req = request(streaming=True, tools=(TOOL,))
    try:
        events = [event async for event in port(client, req).stream(req)]
    finally:
        await client.aclose()
    assert dispatched == ["example.test"]
    assert len([event for event in events if event.kind == "final"]) == 1
    return events[-1].result


@pytest.mark.asyncio
@pytest.mark.parametrize("done", [False, True])
@pytest.mark.parametrize("finish,expected", [
    ("length", "output_limit"), ("max_tokens", "output_limit"),
    ("max_output_tokens", "output_limit"), ("output_limit", "output_limit"),
    ("incomplete", "output_limit"), ("content_filter", "policy_abort"),
    ("policy_abort", "policy_abort"), ("context_length_exceeded", "context_overflow"),
    ("context_overflow", "context_overflow"), ("canceled", "user_abort"),
    ("cancelled", "user_abort"), ("user_abort", "user_abort"),
])
async def test_explicit_non_success_finish_does_not_become_transport_retry(finish, expected, done):
    result = await result_from(body(finish=finish, done=done))
    assert result.response_kind == expected
    assert result.finish_reason == finish
    assert result.tool_calls == ()
    assert result.usage.output_tokens == 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, "stop", "tool_calls"])
@pytest.mark.parametrize("done", [False, True])
async def test_missing_or_success_finish_never_promotes_unfilled_arguments(finish, done):
    result = await result_from(body(finish=finish, done=done))
    assert result.response_kind == "provider_retryable_error"
    assert result.failure_kind == "incomplete_response"
    assert result.tool_calls == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["{", '{"value":', "{}"])
async def test_nonempty_argument_fragments_keep_existing_length_precedence(arguments):
    result = await result_from(body(finish="length", arguments=arguments))
    assert result.response_kind == "output_limit"
    assert result.tool_calls == ()


@pytest.mark.asyncio
async def test_observed_finish_does_not_fabricate_missing_usage():
    result = await result_from(body(finish="length", usage=False))
    assert result.response_kind == "output_limit"
    assert result.usage is None
    assert result.tool_calls == ()


@pytest.mark.asyncio
async def test_length_uses_existing_continuation_pool_without_tool_execution():
    result = await result_from(body(finish="length"))
    config = RunConfiguration(
        profile=ModelProfile(profile_id="local", profile_version="1", provider_family="test",
                             max_continuations=1, max_provider_retries=1),
        limits=RunLimits(max_output_restarts=0), contract=TaskContract(revision="1", items=()),
    )
    before = RunSnapshot(scope=SCOPE, run_id="run", session_id="s", turn_id="t", goal="goal",
                         phase=RunPhase.REQUESTING_MODEL)
    after = reduce_model(before, result, config)
    assert (after.model_calls, after.tool_calls) == (1, 0)
    assert (after.provider_errors, after.continuations) == (0, 1)
    final = reduce_model(after, result, config)
    assert final.reason == "continuation_budget_exhausted"
    assert final.model_calls == 2 and final.tool_calls == 0
    retry = normalize_response(request_id="r", attempt_id="a", stream_complete=False)
    retried = reduce_model(before, retry, config)
    assert (retried.provider_errors, retried.continuations, retried.model_calls) == (1, 0, 1)
    exhausted = reduce_model(retried, retry, config)
    assert exhausted.reason == "provider_retryable_error_retry_exhausted"
