"""Raw provider alternatives and identities must not become mixed Native facts."""

import copy
import json

import httpx
import pytest

from ksadk.harness.native.model_runtime import exchange
from ksadk.model_gateway.client import ModelGatewayClient
from tests.studio.test_model_client import AllowNetwork
from tests.studio.test_model_wire_usage import sse
from tests.studio.test_native_model_port import MODEL, TOOL, port, request

WIRES = ("chat", "responses", "anthropic")


def frames(wire):
    if wire == "chat":
        return [
            {"id": "response-one", "model": "provider-alias", "choices": [
                {"index": 0, "delta": {"content": "42"}, "finish_reason": None},
            ], "usage": {"prompt_tokens": 12, "completion_tokens": 4}},
            {"id": "response-one", "model": "provider-alias", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"},
            ]},
            "[DONE]",
        ]
    if wire == "responses":
        return [
            {"type": "response.created", "response": {
                "id": "response-one", "model": "provider-alias",
            }},
            {"type": "response.output_text.delta", "response_id": "response-one", "delta": "42"},
            {"type": "response.completed", "response": {
                "id": "response-one", "model": "provider-alias",
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }},
        ]
    return [
        {"type": "message_start", "message": {
            "id": "response-one", "model": "provider-alias",
            "usage": {"input_tokens": 12, "output_tokens": 0},
        }},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "42"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]


async def collect(monkeypatch, wire, events, *, streaming=True, emit=None, public_summary=False):
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    calls = []

    def handler(incoming):
        calls.append(incoming)
        if streaming:
            if isinstance(events, httpx.AsyncByteStream):
                return httpx.Response(200, stream=events)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(events),
            )
        return httpx.Response(200, json=events)

    gateway = ModelGatewayClient(
        network_guard=AllowNetwork(), transport=httpx.MockTransport(handler),
    )
    current = request(streaming=streaming, tools=(TOOL,))
    model = MODEL.model_copy(update={"wire_api": wire, "parameters": MODEL.parameters.model_copy(
        update={"public_thinking_summary": public_summary},
    )})
    adapter = port(gateway, current, model=model)
    try:
        result = await exchange(adapter, current, emit=emit)
        assert len(calls) == 1
        return result
    finally:
        await gateway.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
async def test_stable_provider_alias_and_optional_identity_remain_compatible(monkeypatch, wire):
    result = await collect(monkeypatch, wire, frames(wire))
    assert result.response_kind == "final_candidate" and result.text == "42", result


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("field", ["id", "model"])
async def test_one_attempt_does_not_mix_provider_response_identity(monkeypatch, wire, field):
    events = frames(wire)
    if wire == "chat":
        events[1][field] = "PRIVATE_FOREIGN"
    elif wire == "responses":
        events[-1]["response"][field] = "PRIVATE_FOREIGN"
    else:
        foreign = copy.deepcopy(events[0])
        foreign["message"][field] = "PRIVATE_FOREIGN"
        events.insert(1, foreign)
    result = await collect(monkeypatch, wire, events)
    assert result.failure_kind == "invalid_response", result
    assert result.gateway_error_code == "MODEL_RESPONSE_INVALID"
    assert not result.tool_calls
    assert "PRIVATE_FOREIGN" not in result.model_dump_json()
    if wire == "chat":
        assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)
    elif wire == "anthropic":
        assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 0)
    else:
        assert result.usage is None, "A foreign response's counters are not this attempt's evidence"


@pytest.mark.asyncio
async def test_responses_explicit_response_id_is_checked_before_text_projection(monkeypatch):
    events = frames("responses")
    events[1]["response_id"] = "PRIVATE_FOREIGN"
    result = await collect(monkeypatch, "responses", events)
    assert result.failure_kind == "invalid_response", result
    assert "PRIVATE_FOREIGN" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("bad", ["", " ", True, 7])
async def test_supplied_response_identity_is_genuine_nonblank_text(monkeypatch, wire, bad):
    events = frames(wire)
    if wire == "chat":
        events[0]["id"] = bad
    elif wire == "responses":
        events[0]["response"]["id"] = bad
    else:
        events[0]["message"]["id"] = bad
    result = await collect(monkeypatch, wire, events)
    assert result.failure_kind == "invalid_response", result
    assert result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("raw", ["{PRIVATE_BROKEN", "[]", "null", "7"])
async def test_invalid_data_frame_cannot_be_skipped_as_if_it_never_happened(monkeypatch, wire, raw):
    events = frames(wire)
    events.insert(1, raw)
    result = await collect(monkeypatch, wire, events)
    assert result.failure_kind == "invalid_response", result
    assert "PRIVATE_BROKEN" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_chat_projects_choice_zero_without_merging_alternative_text_or_tools(
    monkeypatch, streaming,
):
    zero = {"index": 0, "delta": {"content": "42"},
            "message": {"content": "42"}, "finish_reason": "stop"}
    other = {"index": 1, "delta": {"content": "PRIVATE_ALTERNATIVE", "tool_calls": [
        {"index": 0, "id": "other-call", "function": {"name": "read_value", "arguments": "{}"}},
    ]}, "message": {"content": "PRIVATE_ALTERNATIVE"}, "finish_reason": "tool_calls"}
    packet = {"id": "response-one", "choices": [other, zero]}
    events = [packet, "[DONE]"] if streaming else packet
    result = await collect(monkeypatch, "chat", events, streaming=streaming)
    assert result.response_kind == "final_candidate" and result.text == "42", result
    assert not result.tool_calls


@pytest.mark.asyncio
async def test_chat_does_not_promote_an_alternative_only_frame_to_the_selected_choice(monkeypatch):
    events = frames("chat")
    events[0]["choices"][0]["delta"]["content"] = "4"
    events.insert(1, {"id": "response-one", "choices": [
        {"index": 1, "delta": {"content": "PRIVATE_ALTERNATIVE"}, "finish_reason": "stop"},
    ]})
    events[2]["choices"][0]["delta"]["content"] = "2"
    result = await collect(monkeypatch, "chat", events)
    assert result.response_kind == "final_candidate" and result.text == "42", result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, "0", -1])
async def test_chat_choice_index_is_not_coerced(monkeypatch, bad):
    events = frames("chat")
    events[0]["choices"][0]["index"] = bad
    result = await collect(monkeypatch, "chat", events)
    assert result.failure_kind == "invalid_response", result


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_chat_tool_id_is_stable_identity_not_argument_text(monkeypatch, changed):
    events = frames("chat")
    events[0]["choices"][0]["delta"] = {"tool_calls": [{
        "index": 0, "id": "call-one", "function": {"name": "read_value", "arguments": "{"},
    }]}
    events[1]["choices"][0]["delta"] = {"tool_calls": [{
        "index": 0, "id": "PRIVATE_FOREIGN" if changed else "call-one",
        "function": {"arguments": "}"},
    }]}
    events[1]["choices"][0]["finish_reason"] = "tool_calls"
    result = await collect(monkeypatch, "chat", events)
    if changed:
        assert result.failure_kind == "invalid_response", result
        assert not result.tool_calls
        assert "PRIVATE_FOREIGN" not in result.model_dump_json()
    else:
        assert result.response_kind == "tool_calls", result
        assert result.tool_calls[0].call_id == "call-one"
        assert result.tool_calls[0].name == "read_value" and result.tool_calls[0].arguments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES)
async def test_invalid_visible_content_is_not_stringified_into_an_answer(monkeypatch, wire):
    events = frames(wire)
    if wire == "chat":
        events[0]["choices"][0]["delta"]["content"] = {"PRIVATE_OBJECT": 7}
    elif wire == "responses":
        events[1]["delta"] = {"PRIVATE_OBJECT": 7}
    else:
        events[1]["delta"]["text"] = {"PRIVATE_OBJECT": 7}
    result = await collect(monkeypatch, wire, events)
    assert result.failure_kind == "invalid_response", result
    assert "PRIVATE_OBJECT" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tail", ["foreign", "malformed", "conflicting_finish"])
async def test_chat_finish_with_usage_does_not_seal_before_the_response_boundary(
    monkeypatch, bad_tail,
):
    events = frames("chat")
    events[0]["choices"][0]["delta"] = {"tool_calls": [{
        "index": 0, "id": "call-one",
        "function": {"name": "read_value", "arguments": "{}"},
    }]}
    events[1]["choices"][0]["finish_reason"] = "tool_calls"
    events[1]["usage"] = {"prompt_tokens": 12, "completion_tokens": 4}
    tail = copy.deepcopy(events[1])
    if bad_tail == "foreign":
        tail["id"] = "PRIVATE_FOREIGN"
    elif bad_tail == "malformed":
        tail = "{PRIVATE_BROKEN"
    else:
        tail["choices"][0]["finish_reason"] = "stop"
    events.insert(-1, tail)
    result = await collect(monkeypatch, "chat", events)
    assert result.failure_kind == "invalid_response", result
    assert not result.tool_calls, "An unsealed response cannot authorize a tool batch"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)
    assert "PRIVATE_" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_chat_post_finish_usage_updates_are_consumed_before_sealing(monkeypatch):
    events = frames("chat")
    events[1]["usage"] = {"prompt_tokens": 12, "completion_tokens": 4}
    events.insert(-1, {"id": "response-one", "choices": [],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 7}})
    result = await collect(monkeypatch, "chat", events)
    assert result.response_kind == "final_candidate" and result.text == "42", result
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 7)


@pytest.mark.asyncio
async def test_chat_finish_before_authoritative_tool_snapshot_keeps_its_finish_reason(monkeypatch):
    events = frames("chat")
    events[0]["choices"][0]["delta"] = {"tool_calls": [{
        "index": 0, "id": "call-one", "function": {"name": "read_value"},
    }]}
    events[1]["choices"][0]["finish_reason"] = "tool_calls"
    events.insert(-1, {"id": "response-one", "choices": [{
        "index": 0, "message": {"tool_calls": [{
            "index": 0, "id": "call-one",
            "function": {"name": "read_value", "arguments": {}},
        }]},
    }]})
    result = await collect(monkeypatch, "chat", events)
    assert result.response_kind == "tool_calls", result
    assert result.tool_calls[0].call_id == "call-one" and result.tool_calls[0].arguments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["done", "eof"])
async def test_chat_known_finish_with_optional_usage_can_seal_at_done_or_eof(monkeypatch, boundary):
    events = frames("chat")
    if boundary == "eof":
        events.pop()
    result = await collect(monkeypatch, "chat", events)
    assert result.response_kind == "final_candidate" and result.text == "42", result
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)


@pytest.mark.asyncio
async def test_chat_read_failure_after_finish_keeps_usage_without_a_tool_batch(monkeypatch):
    class Interrupted(httpx.AsyncByteStream):
        closed = 0

        async def __aiter__(self):
            events = frames("chat")[:-1]
            events[0]["choices"][0]["delta"] = {"tool_calls": [{
                "index": 0, "id": "call-one",
                "function": {"name": "read_value", "arguments": "{}"},
            }]}
            events[1]["choices"][0]["finish_reason"] = "tool_calls"
            events[1]["usage"] = {"prompt_tokens": 12, "completion_tokens": 4}
            yield sse(events)
            raise httpx.ReadError("PRIVATE_TRANSPORT_BODY")

        async def aclose(self):
            self.closed += 1

    stream = Interrupted()
    result = await collect(monkeypatch, "chat", stream)
    assert result.response_kind in {"provider_terminal_error", "provider_retryable_error"}, result
    assert not result.tool_calls
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 4)
    assert stream.closed == 1 and "PRIVATE_TRANSPORT_BODY" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["chat", "responses", "anthropic", "chat_after_finish"])
async def test_ordinary_studio_rejects_foreign_wire_identity_without_retry_or_tools(
    tmp_path, monkeypatch, case,
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

    wire = "chat" if case == "chat_after_finish" else case
    events = frames(wire)
    if case == "chat_after_finish":
        events[1]["usage"] = {"prompt_tokens": 12, "completion_tokens": 4}
        foreign = copy.deepcopy(events[1])
        foreign["id"] = "PRIVATE_FOREIGN"
        events.insert(-1, foreign)
    elif wire == "chat":
        events[1]["id"] = "PRIVATE_FOREIGN"
    elif wire == "responses":
        events[-1]["response"]["id"] = "PRIVATE_FOREIGN"
    else:
        foreign = copy.deepcopy(events[0])
        foreign["message"]["id"] = "PRIVATE_FOREIGN"
        events.insert(1, foreign)
    calls = []

    def handler(incoming):
        calls.append(incoming)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(events),
        )

    class Gateway(ModelGatewayClient):
        def supports_streaming(self, model, *, requested=True):
            return requested

    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    gateway = Gateway(network_guard=AllowNetwork(), transport=httpx.MockTransport(handler))
    studio = StudioService(
        tmp_path / "studio", model_client=gateway, native_harness_agent_ids=("wire-agent",),
    )
    try:
        studio.create_agent(
            agent_id="wire-agent", name="Native wire identity",
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
        assert result.status == RunStatus.FAILED, result.error
        assert len(calls) == 1 and json.loads(calls[0].content)["stream"] is True
        assert "PRIVATE_FOREIGN" not in result.model_dump_json()
        session = await studio.session_service.get_session_metadata("wire-session")
        scope = RunScope(
            tenant_id="local-studio", workspace_id=studio.workspace_record.workspace_id,
            user_id=session.user_id, agent_id=session.agent_id,
        )
        composer = studio._native_product_composer
        head = composer.store.session_head(scope, "wire-session")
        events = composer.store.events(scope, head.run_id)
        assert head.model_calls == 1 and head.tool_calls == 0
        assert sum(event.kind == "run.failed" for event in events) == 1
        assert not any(event.kind == "run.completed" for event in events)
        receipts = [event for event in events if event.kind == "usage.reported"]
        if wire == "responses":
            assert not receipts and result.usage.reported is False
        else:
            assert len(receipts) == 1 and result.usage.reported is True
            assert (result.usage.input_tokens, result.usage.output_tokens) == (
                12, 0 if wire == "anthropic" else 4,
            )
    finally:
        await studio.aclose()
