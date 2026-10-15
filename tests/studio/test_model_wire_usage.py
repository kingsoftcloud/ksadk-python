"""Provider bytes must not manufacture a complete, coerced usage report."""

import json

import httpx
import pytest

from ksadk.harness.native.model_runtime import exchange
from ksadk.model_gateway.client import ModelGatewayClient
from tests.studio.test_model_client import AllowNetwork
from tests.studio.test_native_model_port import MODEL, port, request

WIRES = ("chat", "responses", "anthropic")


def payload(wire, usage):
    if wire == "chat":
        value = {"choices": [{"message": {"content": "42"}, "finish_reason": "stop"}]}
    elif wire == "responses":
        value = {"output_text": "42", "status": "completed", "output": [{
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "42"}],
        }]}
    else:
        value = {"content": [{"type": "text", "text": "42"}], "stop_reason": "end_turn"}
    if usage is not None:
        value["usage"] = usage
    return value


def usage_for(wire, value):
    if wire != "chat" or not isinstance(value, dict):
        return value
    names = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens"}
    return {names.get(key, key): item for key, item in value.items()}


def stream_events(wire, usage):
    if wire == "chat":
        return [
            {"choices": [{"delta": {"content": "42"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage},
            "[DONE]",
        ]
    if wire == "responses":
        return [
            {"type": "response.output_text.delta", "delta": "42"},
            {"type": "response.completed", "response": {"usage": usage}},
        ]
    if isinstance(usage, dict):
        initial = {key: value for key, value in usage.items() if key != "output_tokens"}
        final = {key: value for key, value in usage.items() if key == "output_tokens"}
    else:
        initial, final = usage, None
    return [
        {"type": "message_start", "message": {"usage": initial}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "42"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": final},
        {"type": "message_stop"},
    ]


def sse(events):
    return "".join(
        "data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n"
        for event in events
    ).encode()


async def model_result(monkeypatch, wire, *, streaming, usage=None, events=None):
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    calls = []

    def handler(incoming):
        calls.append(incoming)
        if streaming:
            body = sse(events if events is not None else stream_events(wire, usage))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        return httpx.Response(200, json=payload(wire, usage))

    gateway = ModelGatewayClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(handler),
    )
    current = request(streaming=streaming)
    model = MODEL.model_copy(update={"wire_api": wire})
    try:
        result = await exchange(port(gateway, current, model=model), current)
        assert len(calls) == 1
        assert json.loads(calls[0].content).get("stream", False) is streaming
        return result
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("value", [None, {}, {"input_tokens": 12}, {"total_tokens": 16}])
async def test_absent_or_partial_wire_usage_stays_unknown(monkeypatch, wire, streaming, value):
    result = await model_result(
        monkeypatch, wire, streaming=streaming, usage=usage_for(wire, value),
    )
    assert result.response_kind == "final_candidate", result
    assert result.text == "42"
    assert result.usage is None, "Missing counters are not an explicit provider report of zero"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("counts", [(0, 0), (12, 4)])
async def test_complete_wire_usage_including_zero_is_reported(monkeypatch, wire, streaming, counts):
    result = await model_result(
        monkeypatch, wire, streaming=streaming,
        usage=usage_for(wire, {"input_tokens": counts[0], "output_tokens": counts[1]}),
    )
    assert result.response_kind == "final_candidate", result
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("bad", [True, "12", 12.5, -1, None])
async def test_malformed_counter_is_not_coerced_or_retried(monkeypatch, wire, streaming, bad):
    result = await model_result(
        monkeypatch, wire, streaming=streaming,
        usage=usage_for(wire, {"input_tokens": bad, "output_tokens": 4}),
    )
    assert result.response_kind == "provider_terminal_error", result
    assert result.failure_kind == "invalid_response"
    assert result.gateway_error_code == "MODEL_RESPONSE_INVALID"
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("bad", [["PRIVATE_USAGE"], [], 0, False])
async def test_invalid_usage_container_is_a_sanitized_protocol_error(
    monkeypatch, wire, streaming, bad,
):
    result = await model_result(monkeypatch, wire, streaming=streaming, usage=bad)
    assert result.failure_kind == "invalid_response", result
    assert result.gateway_error_code == "MODEL_RESPONSE_INVALID"
    assert "PRIVATE_USAGE" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_inclusive_input_counts_cache_reads_and_writes(monkeypatch, streaming):
    result = await model_result(
        monkeypatch, "anthropic", streaming=streaming,
        usage={"input_tokens": 12, "output_tokens": 4,
               "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3},
    )
    assert result.response_kind == "final_candidate", result
    assert result.usage.input_tokens == 22
    assert result.usage.output_tokens == 4 and result.usage.cached_input_tokens == 7


@pytest.mark.asyncio
async def test_anthropic_partial_updates_do_not_erase_known_fields(monkeypatch):
    events = stream_events("anthropic", {"input_tokens": 12, "output_tokens": 4})
    events.insert(-1, {"type": "message_delta", "delta": {}, "usage": {}})
    result = await model_result(monkeypatch, "anthropic", streaming=True, events=events)
    assert result.response_kind == "final_candidate", result
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 4


@pytest.mark.parametrize("wire", WIRES)
def test_buffered_dto_marks_known_counters_and_derives_missing_total(wire):
    from ksadk.studio.model_client import OpenAICompatibleModelClient

    raw_usage = usage_for(wire, {"input_tokens": 12, "output_tokens": 4})
    parser = {
        "chat": OpenAICompatibleModelClient._parse_response,
        "responses": OpenAICompatibleModelClient._parse_responses_response,
        "anthropic": OpenAICompatibleModelClient._parse_anthropic_response,
    }[wire]
    result = parser(httpx.Response(200, json=payload(wire, raw_usage)))
    assert result.usage.reported is True and result.usage.source == "model-provider"
    assert result.usage.total_tokens == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_last", [False, True])
async def test_chat_usage_only_frame_before_finish_is_preserved(monkeypatch, bad_last):
    events = stream_events("chat", None)
    events.insert(1, {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4}})
    if bad_last:
        events[2]["usage"] = {"prompt_tokens": "PRIVATE_BAD_COUNTER", "completion_tokens": 4}
    result = await model_result(monkeypatch, "chat", streaming=True, events=events)
    assert result.response_kind == ("provider_terminal_error" if bad_last else "final_candidate")
    if bad_last:
        assert result.failure_kind == "invalid_response"
        assert "PRIVATE_BAD_COUNTER" not in result.model_dump_json()
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["chat", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("bad", [True, "7", None, -1])
async def test_optional_subdivision_does_not_coerce_reported_counter(
    monkeypatch, wire, streaming, bad,
):
    details = "prompt_tokens_details" if wire == "chat" else "input_tokens_details"
    result = await model_result(
        monkeypatch, wire, streaming=streaming,
        usage=usage_for(wire, {
            "input_tokens": 12, "output_tokens": 4, details: {"cached_tokens": bad},
        }),
    )
    assert result.failure_kind == "invalid_response", result
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_optional_null_cache_is_absent(monkeypatch, streaming):
    result = await model_result(
        monkeypatch, "anthropic", streaming=streaming,
        usage={"input_tokens": 12, "output_tokens": 4,
               "cache_read_input_tokens": None, "cache_creation_input_tokens": None},
    )
    assert result.response_kind == "final_candidate", result
    assert result.usage.input_tokens == 12 and result.usage.cached_input_tokens == 0


@pytest.mark.asyncio
async def test_anthropic_zero_update_and_cache_breakdown_merge_are_right_biased(monkeypatch):
    events = stream_events("anthropic", {
        "input_tokens": 12, "output_tokens": 4,
        "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3,
    })
    events.insert(-1, {"type": "message_delta", "delta": {}, "usage": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": None,
    }})
    result = await model_result(monkeypatch, "anthropic", streaming=True, events=events)
    assert result.response_kind == "final_candidate", result
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 0
    assert result.usage.cached_input_tokens == 7


@pytest.mark.asyncio
async def test_anthropic_rejected_delta_keeps_the_last_known_report(monkeypatch):
    events = stream_events("anthropic", {"input_tokens": 12, "output_tokens": 4})
    events[0]["message"]["usage"]["output_tokens"] = 0
    events[2]["usage"]["output_tokens"] = "PRIVATE_BAD_COUNTER"
    result = await model_result(monkeypatch, "anthropic", streaming=True, events=events)
    assert result.failure_kind == "invalid_response", result
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 0
    assert "PRIVATE_BAD_COUNTER" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("streaming", [False, True])
async def test_ordinary_studio_commits_true_zero_report_once(
    tmp_path, monkeypatch, wire, streaming,
):
    from ksadk.harness.native.state import RunScope
    from ksadk.studio.contracts import (
        AgentSpec,
        Instructions,
        ModelSpec,
        NetworkPolicy,
        RunStatus,
        RuntimeRef,
        SecuritySpec,
    )
    from ksadk.studio.service import StudioService
    from tests.studio.test_dsh_provider_registration import _managed_profile

    calls = []
    usage = usage_for(wire, {"input_tokens": 0, "output_tokens": 0})

    def handler(incoming):
        calls.append(incoming)
        if streaming:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=sse(stream_events(wire, usage)),
            )
        return httpx.Response(200, json=payload(wire, usage))

    class Gateway(ModelGatewayClient):
        def supports_streaming(self, model, *, requested=True):
            return streaming and requested

    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    gateway = Gateway(network_guard=AllowNetwork(), transport=httpx.MockTransport(handler))
    studio = StudioService(
        tmp_path / "studio", model_client=gateway, native_harness_agent_ids=("wire-agent",),
    )
    try:
        studio.create_agent(
            agent_id="wire-agent", name="Native wire accounting",
            spec=AgentSpec(
                runtime=RuntimeRef(type="harness"),
                instructions=Instructions(system="Answer the request."),
                model=ModelSpec(
                    model="fixture-model", endpoint_url="https://example.test/v1/model",
                    wire_api=wire, credential_ref="env://FIXTURE_KEY",
                ),
                security=SecuritySpec(
                    allowed_permissions=["process:host-user"],
                    network=NetworkPolicy(allowed_hosts=["example.test"]),
                ),
            ),
        )
        build = await studio.ensure_current_build("wire-agent")
        result = await studio.run_build(build.id, "Return 42.", "wire-session")
        assert result.status == RunStatus.COMPLETED and result.output == "42", result.error
        assert result.usage.reported is True
        assert result.usage.input_tokens == result.usage.output_tokens == 0
        assert len(calls) == 1
        assert json.loads(calls[0].content).get("stream", False) is streaming
        session = await studio.session_service.get_session_metadata("wire-session")
        scope = RunScope(
            tenant_id="local-studio", workspace_id=studio.workspace_record.workspace_id,
            user_id=session.user_id, agent_id=session.agent_id,
        )
        composer = studio._native_product_composer
        head = composer.store.session_head(scope, "wire-session")
        events = composer.store.events(scope, head.run_id)
        assert head.model_calls == 1 and head.tool_calls == 0
        assert sum(event.kind == "usage.reported" for event in events) == 1
        assert not any(event.kind == "usage.unavailable" for event in events)
        assert sum(event.kind == "run.completed" for event in events) == 1
    finally:
        await studio.aclose()
