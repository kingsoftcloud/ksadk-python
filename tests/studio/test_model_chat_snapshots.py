"""Chat snapshot extension must supplement facts, never overwrite them."""

import copy

import httpx
import pytest

from ksadk.model_gateway.client import ModelGatewayClient
from ksadk.model_gateway.events import ModelStreamEventType as Kind
from tests.studio.test_model_client import AllowNetwork
from tests.studio.test_model_wire_identity import collect, frames
from tests.studio.test_model_wire_usage import sse
from tests.studio.test_native_model_port import MODEL, NETWORK


def snapshot(message, *, placement="choice"):
    return ({"id": "response-one", "message": message} if placement == "root" else {
        "id": "response-one", "choices": [{"index": 0, "message": message}],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["choice", "root"])
@pytest.mark.parametrize("prefix", ["", "4", "42"])
async def test_full_chat_snapshot_fills_a_matching_tail(monkeypatch, placement, prefix):
    values = frames("chat")
    values[0]["choices"][0]["delta"]["content"] = prefix
    values.insert(-1, snapshot({"content": "42"}, placement=placement))
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "final_candidate" and result.text == "42", result


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["choice", "root"])
@pytest.mark.parametrize("case", ["rewrite", "truncate", "post_finish_delta", "post_snapshot_delta",
                                 "changed_snapshot", "mixed_aliases"])
async def test_chat_snapshot_or_finish_cannot_rewrite_observed_content(
    monkeypatch, placement, case,
):
    values = frames("chat")
    if case in {"rewrite", "truncate"}:
        values.insert(-1, snapshot({"content": "PRIVATE_CHANGED" if case == "rewrite" else ""},
                                   placement=placement))
    elif case == "post_finish_delta":
        late = copy.deepcopy(values[0])
        late["choices"][0]["delta"]["content"] = "PRIVATE_CHANGED"
        values.insert(-1, late)
    elif case == "post_snapshot_delta":
        values.insert(1, snapshot({"content": "42"}, placement=placement))
        late = copy.deepcopy(values[0])
        late["choices"][0]["delta"]["content"] = "PRIVATE_CHANGED"
        values.insert(2, late)
    elif case == "changed_snapshot":
        values.insert(-1, snapshot({"content": "42"}, placement=placement))
        values.insert(-1, snapshot({"content": "42PRIVATE_CHANGED"}, placement=placement))
    else:
        values[0]["choices"][0]["delta"].update(reasoning="PRIVATE_CHANGED",
                                                reasoning_content="original")
    result = await collect(monkeypatch, "chat", values)
    assert result.failure_kind == "invalid_response" and not result.tool_calls, result
    assert "PRIVATE_CHANGED" not in result.model_dump_json()
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)


@pytest.mark.asyncio
async def test_duplicate_chat_snapshot_does_not_duplicate_output(monkeypatch):
    values = frames("chat")
    values[0]["choices"][0]["delta"]["content"] = "4"
    values.insert(-1, snapshot({"content": "42"}))
    values.insert(-1, snapshot({"content": "42"}))
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "final_candidate" and result.text == "42", result


@pytest.mark.asyncio
async def test_snapshot_from_an_alternate_choice_cannot_supply_choice_zero_text(monkeypatch):
    values = frames("chat")
    foreign = snapshot({"content": "PRIVATE_CHANGED"})
    foreign["choices"][0]["index"] = 1
    values.insert(-1, foreign)
    result = await collect(monkeypatch, "chat", values)
    assert result.response_kind == "final_candidate" and result.text == "42", result


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["choice", "root"])
async def test_reasoning_snapshot_remains_a_separate_channel(monkeypatch, placement):
    values = frames("chat")
    values[0]["choices"][0]["delta"]["reasoning_content"] = "rea"
    values.insert(-1, snapshot({"content": "42", "reasoning_content": "reason"},
                               placement=placement))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    gateway = ModelGatewayClient(network_guard=AllowNetwork(), transport=httpx.MockTransport(
        lambda incoming: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                       content=sse(values)),
    ))
    try:
        events = [e async for e in gateway.stream_events(MODEL, messages=[],
                    network_policy=NETWORK, timeout_seconds=10)]
        assert events[-1].type is Kind.TURN_COMPLETED, events
        assert "".join(e.text for e in events if e.type is Kind.TEXT_DELTA) == "42"
        assert "".join(e.text for e in events if e.type is Kind.REASONING_DELTA) == "reason"
    finally:
        await gateway.aclose()
