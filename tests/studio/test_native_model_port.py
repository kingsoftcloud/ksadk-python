"""Studio ModelGateway -> Native sealed model-result projection."""

from __future__ import annotations

import pytest

from ksadk.harness.native.model import ModelProfile
from ksadk.harness.native.ports import ModelRequest, ToolDefinition
from ksadk.harness.native.state import RunScope
from ksadk.model_gateway.events import ModelStreamEvent, ModelStreamEventType
from ksadk.studio.contracts import (
    ModelParameters,
    NetworkPolicy,
    ResolvedModel,
    SecuritySpec,
    Usage,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.native_model_port import (
    StudioNativeModelPort,
    native_model_profile_from_build,
    native_studio_streaming_support,
)

SCOPE = RunScope(tenant_id="tenant", workspace_id="workspace", user_id="user", agent_id="agent")
MODEL = ResolvedModel(
    provider="openai-compatible",
    model="fixture",
    endpoint_url="https://example.test/v1/chat/completions",
    credential_ref="env://FIXTURE_KEY",
    parameters=ModelParameters(max_tokens=512),
)
NETWORK = NetworkPolicy(allowed_hosts=["example.test"])
TOOL = ToolDefinition(
    name="read_value",
    description="Read a value",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)


def request(*, streaming=False, tools=()):
    profile = ModelProfile(
        profile_id="fixture",
        profile_version="1",
        provider_family="openai-compatible",
        context_window=4096,
        max_output_tokens=512,
        streaming=streaming,
    )
    return ModelRequest(
        scope=SCOPE,
        run_id="run",
        request_id="request",
        attempt_id="attempt",
        profile=profile,
        instructions="Do the fixture.",
        messages=({"role": "user", "content": "Read the value."},),
        tools=tools,
    )


class Client:
    def __init__(self, *, response=None, error=None, events=()):
        self.response, self.error, self.events = response, error, events
        self.calls = []

    async def complete(self, model, **kwargs):
        self.calls.append((model, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    async def stream_events(self, model, **kwargs):
        self.calls.append((model, kwargs))
        for event in self.events:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "consumer_close"])
async def test_native_stream_joins_gateway_iterator_before_returning(terminal):
    class OwnedStreamClient(Client):
        closed = False

        async def stream_events(self, model, **kwargs):
            try:
                yield ModelStreamEvent(
                    ModelStreamEventType.TEXT_DELTA, model.model, 1, text="partial"
                )
                if terminal == "failed":
                    yield ModelStreamEvent(
                        ModelStreamEventType.TURN_FAILED, model.model, 2,
                        failure_category="malformed_tool_call",
                        gateway_error_code="MODEL_TOOL_PROTOCOL_INVALID",
                    )
                else:
                    yield ModelStreamEvent(
                        ModelStreamEventType.TURN_COMPLETED, model.model, 2,
                        metadata={"finish_reason": "stop"},
                    )
            finally:
                self.closed = True

    client = OwnedStreamClient()
    input_request = request(streaming=True)
    iterator = port(client, input_request).stream(input_request)
    if terminal == "consumer_close":
        assert (await anext(iterator)).kind == "text_delta"
        await iterator.aclose()
    else:
        events = [event async for event in iterator]
        assert events[-1].kind == "final"
    assert client.closed, "The model effect still owns an unclosed Gateway iterator"


def port(client, input_request, *, model=MODEL):
    return StudioNativeModelPort(
        client,
        scope=SCOPE,
        model=model,
        network_policy=NETWORK,
        profile_digest=input_request.profile.digest,
    )


def resolved_build(*, agent_id="agent", models=(MODEL,)):
    return {
        "agentId": agent_id,
        "model": models[0].model_dump(by_alias=True),
        "fallbackModels": [model.model_dump(by_alias=True) for model in models[1:]],
        "security": SecuritySpec(network=NETWORK).model_dump(by_alias=True),
    }


def test_native_profile_uses_only_locked_build_budgets():
    build = resolved_build()
    build["context"] = {"maxInputTokens": 8192, "reserveOutputTokens": 1024}
    build["execution"] = {
        "retry": {"maxAttempts": 3, "providerMaxAttempts": 5, "backoffSeconds": 1.5}
    }
    profile = native_model_profile_from_build(build, selected_model="fixture", build_id="build-123")
    assert len(profile.profile_version) == 24
    assert profile.profile_id.startswith("studio-")
    assert profile.context_window == 8192
    assert profile.max_output_tokens == 512
    assert profile.max_retries == 2
    assert profile.max_provider_retries == 4
    assert profile.retry_initial_delay_seconds == 1.5
    assert profile.image_input_mode == "text_only"

    narrowed = dict(build)
    narrowed["model"] = MODEL.model_copy(
        update={"metadata": {"context_window_tokens": 4096}}
    ).model_dump(by_alias=True)
    assert (
        native_model_profile_from_build(
            narrowed, selected_model="fixture", build_id="build-123"
        ).context_window
        == 4096
    )

    vision = dict(build)
    vision["model"] = MODEL.model_copy(
        update={"metadata": {"supports_image_input": True}}
    ).model_dump(by_alias=True)
    assert (
        native_model_profile_from_build(
            vision, selected_model="fixture", build_id="build-123"
        ).image_input_mode
        == "native"
    )

    slow = dict(build)
    slow["model"] = MODEL.model_copy(
        update={"metadata": {"request_timeout_seconds": 240}}
    ).model_dump(by_alias=True)
    assert (
        native_model_profile_from_build(
            slow, selected_model="fixture", build_id="build-123"
        ).request_timeout_seconds
        == 240
    )
    slow["execution"] = {"timeoutSeconds": 250}
    with pytest.raises(ValueError, match="request_timeout_invalid"):
        native_model_profile_from_build(slow, selected_model="fixture", build_id="build-123")


def test_native_profile_pins_explicit_studio_reasoning_effort():
    build = resolved_build()
    build["context"] = {"maxInputTokens": 8192, "reserveOutputTokens": 1024}
    build["execution"] = {}
    automatic = native_model_profile_from_build(
        build, selected_model="fixture", build_id="build-123"
    )
    selected = native_model_profile_from_build(
        build, selected_model="fixture", build_id="build-123", reasoning_effort="low"
    )
    assert automatic.reasoning_effort is None
    assert selected.reasoning_effort == "low"
    assert selected.digest != automatic.digest
    with pytest.raises(ValueError, match="reasoning_effort_invalid"):
        native_model_profile_from_build(
            build, selected_model="fixture", build_id="build-123", reasoning_effort="unsupported"
        )


def test_vision_role_selects_only_the_locked_vision_model_even_with_same_model_name():
    vision = MODEL.model_copy(update={"metadata": {"supports_image_input": True}})
    build = resolved_build()
    build["visionModel"] = vision.model_dump(by_alias=True)
    build["context"] = {"maxInputTokens": 8192, "reserveOutputTokens": 1024}
    build["execution"] = {}
    agent_profile = native_model_profile_from_build(
        build, selected_model="fixture", build_id="build-123"
    )
    vision_profile = native_model_profile_from_build(
        build, selected_model="fixture", build_id="build-123", model_role="vision"
    )
    assert agent_profile.image_input_mode == "text_only"
    assert vision_profile.image_input_mode == "native"
    assert agent_profile.digest != vision_profile.digest
    bound = StudioNativeModelPort.from_resolved_build(
        Client(),
        scope=SCOPE,
        resolved_agent_spec=build,
        selected_model="fixture",
        profile_digest=vision_profile.digest,
        model_role="vision",
    )
    assert bound._model.metadata["supports_image_input"] is True


def test_native_profile_uses_studio_gateway_streaming_decision(monkeypatch):
    class Gateway(Client):
        def supports_streaming(self, model, *, requested):
            assert model.model == "fixture"
            self.requested = requested
            return requested

    gateway = Gateway()
    build = resolved_build()
    build["context"] = {"maxInputTokens": 8192, "reserveOutputTokens": 1024}
    build["execution"] = {}
    profile = native_model_profile_from_build(
        build,
        selected_model="fixture",
        build_id="build-123",
        streaming_support=lambda model: native_studio_streaming_support(gateway, model),
    )
    assert gateway.requested is True
    assert profile.streaming is True
    assert (
        profile.digest
        != native_model_profile_from_build(
            build, selected_model="fixture", build_id="build-123"
        ).digest
    )

    monkeypatch.setattr(
        "ksadk.studio.native_model_port.provider_compat.prefer_buffered_transport",
        lambda **_kwargs: True,
    )
    assert native_studio_streaming_support(gateway, MODEL) is False
    assert gateway.requested is False
    assert native_studio_streaming_support(Client(), MODEL) is False


def test_native_profile_rejects_missing_or_conflicting_build_budgets():
    build = resolved_build()
    with pytest.raises(ValueError, match="budget_unavailable"):
        native_model_profile_from_build(build, selected_model="fixture", build_id="build-123")
    build["context"] = {"maxInputTokens": 1024, "reserveOutputTokens": 512}
    build["execution"] = {}
    build["model"] = MODEL.model_copy(
        update={"parameters": ModelParameters(max_tokens=2048)}
    ).model_dump(by_alias=True)
    with pytest.raises(ValueError, match="budget_conflict"):
        native_model_profile_from_build(build, selected_model="fixture", build_id="build-123")


def response(*, text="answer", finish="stop", calls=(), reported=True):
    return ModelResponse(
        content=text,
        finish_reason=finish,
        usage=Usage(input_tokens=10, output_tokens=3, total_tokens=13, reported=reported),
        tool_calls=list(calls),
        raw_message={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("image_mode", ["native", "text_only"])
async def test_studio_gateway_projects_host_image_after_complete_tool_batch(image_mode):
    base = request()
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}
    observed = base.model_copy(
        update={
            "profile": base.profile.model_copy(update={"image_input_mode": image_mode}),
            "messages": (
                {"role": "user", "content": "Inspect the screenshot."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "capture-1",
                            "type": "function",
                            "function": {"name": "capture", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "capture-1",
                    "content": [
                        {"type": "text", "text": '{"status":"ok"}'},
                        image,
                    ],
                },
            ),
        }
    )
    client = Client(response=response())
    await port(client, observed).complete(observed)
    messages = client.calls[0][1]["messages"]
    if image_mode == "native":
        assert messages[-2]["role"] == "tool"
        assert isinstance(messages[-2]["content"], str)
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][1]["image_url"]["url"] == image["image_url"]["url"]
    else:
        assert len(messages) == 4
        assert messages[-1]["role"] == "tool"
        assert "image content is unavailable" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_studio_native_model_port_applies_host_output_limit():
    base = request()
    capped = base.model_copy(update={"max_output_tokens": 64})
    client = Client(response=response())
    result = await port(client, base).complete(capped)
    assert result.response_kind == "final_candidate"
    assert client.calls[0][1]["max_output_tokens"] == 64
    with pytest.raises(ValueError, match="exceeds pinned profile"):
        ModelRequest.model_validate({**base.model_dump(mode="json"), "max_output_tokens": 513})


@pytest.mark.asyncio
async def test_studio_native_model_port_preserves_length_and_usage():
    input_request = request()
    client = Client(response=response(text="partial", finish="length"))
    result = await port(client, input_request).complete(input_request)
    assert result.response_kind == "output_limit"
    assert result.text == "partial"
    assert result.usage.input_tokens == 10
    assert client.calls[0][1]["max_attempts"] == 1
    assert client.calls[0][1]["retry_on_length"] is False
    assert client.calls[0][1]["retry_reasoning_only"] is False
    assert client.calls[0][1]["messages"][0] == {
        "role": "system",
        "content": "Do the fixture.",
    }


@pytest.mark.asyncio
async def test_studio_native_glm_buffered_content_cannot_expose_inline_reasoning():
    input_request = request()
    glm = MODEL.model_copy(update={"model": "glm-5.3-flash"})
    client = Client(response=response(text="The screenshot shows 6428.</think>6428"))
    result = await port(client, input_request, model=glm).complete(input_request)
    assert result.response_kind == "final_candidate"
    assert result.text == "6428"
    other = await port(client, input_request).complete(input_request)
    assert other.text == "The screenshot shows 6428.</think>6428"


@pytest.mark.asyncio
async def test_studio_native_glm_stream_never_exposes_inline_reasoning_delta():
    input_request = request(streaming=True)
    glm = MODEL.model_copy(update={"model": "glm-5.3-flash"})
    client = Client(
        events=(
            ModelStreamEvent(
                ModelStreamEventType.TEXT_DELTA,
                glm.model,
                1,
                text="The screenshot shows 6428.</thi",
            ),
            ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, glm.model, 2, text="nk>64"),
            ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, glm.model, 3, text="28"),
            ModelStreamEvent(
                ModelStreamEventType.TURN_COMPLETED,
                glm.model,
                4,
                metadata={"finish_reason": "stop"},
            ),
        )
    )
    events = [event async for event in port(client, input_request, model=glm).stream(input_request)]
    assert [(event.kind, event.text) for event in events[:-1]] == [
        ("text_delta", "64"),
        ("text_delta", "28"),
    ]
    assert events[-1].result.response_kind == "final_candidate"
    assert events[-1].result.text == "6428"


@pytest.mark.asyncio
async def test_studio_native_model_port_does_not_complete_without_finish_reason():
    input_request = request()
    client = Client(response=response(text="partial", finish=""))
    result = await port(client, input_request).complete(input_request)
    assert result.response_kind == "provider_retryable_error"
    assert result.failure_kind == "incomplete_response"
    assert result.text == "partial"


@pytest.mark.asyncio
async def test_studio_native_model_port_seals_bad_arguments_for_safe_tool_receipt():
    input_request = request(tools=(TOOL,))
    good = Client(
        response=response(
            text="",
            finish="tool_calls",
            calls=(ToolCall(id="call-1", name="read_value", arguments='{"value": 7}'),),
        )
    )
    result = await port(good, input_request).complete(input_request)
    assert result.response_kind == "tool_calls"
    assert result.tool_calls[0].arguments == {"value": 7}
    assert good.calls[0][1]["tools"][0]["function"]["name"] == "read_value"

    bad = Client(
        response=response(
            text="",
            finish="tool_calls",
            calls=(ToolCall(id="call-1", name="read_value", arguments='{"x":1,"x":2}'),),
        )
    )
    malformed = await port(bad, input_request).complete(input_request)
    assert malformed.response_kind == "tool_calls"
    assert malformed.tool_calls[0].call_id == "call-1"
    assert malformed.tool_calls[0].arguments == {}
    assert malformed.tool_calls[0].wire_arguments_invalid is True


@pytest.mark.asyncio
async def test_studio_native_model_port_classifies_gateway_empty_length_and_rate_limit():
    input_request = request()
    length = Client(
        error=StudioError(
            "MODEL_EMPTY_RESPONSE",
            "empty",
            status_code=502,
            details={"finishReason": "length"},
        )
    )
    assert (await port(length, input_request).complete(input_request)).response_kind == (
        "output_limit"
    )
    rate_limit = Client(error=StudioError("MODEL_RATE_LIMITED", "limited", status_code=429))
    limited = await port(rate_limit, input_request).complete(input_request)
    assert limited.response_kind == "provider_retryable_error"
    assert limited.failure_kind == "rate_limit"
    assert limited.provider_failure_category == "rate_limit"
    assert limited.provider_status_code == 429
    invalid = Client(error=StudioError("MODEL_REQUEST_INVALID", "bad request", status_code=400))
    rejected = await port(invalid, input_request).complete(input_request)
    assert rejected.failure_kind == "client_error"
    assert rejected.provider_failure_category == "invalid_request"
    assert rejected.provider_status_code == 400
    assert rejected.gateway_error_code == "MODEL_REQUEST_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "metadata", "expected_kind"),
    [
        ("MODEL_TOOL_PROTOCOL_INVALID", {}, "malformed_tool_call"),
        ("MODEL_EMPTY_RESPONSE", {"finish_reason": "length"}, "output_limit"),
        ("MODEL_EMPTY_RESPONSE", {}, "empty_response"),
        ("MODEL_EMPTY_RESPONSE", {"finish_reason": []}, "empty_response"),
        ("MODEL_RESPONSE_INVALID", {}, "provider_terminal_error"),
        ("MODEL_REQUEST_INVALID", {}, "provider_terminal_error"),
    ],
)
async def test_streamed_host_error_matches_buffered_projection(code, metadata, expected_kind):
    input_request = request(streaming=True)
    client = Client(
        events=(
            ModelStreamEvent(
                ModelStreamEventType.TURN_FAILED,
                "fixture",
                1,
                failure_category="invalid_request",
                gateway_error_code=code,
                status_code=422,
                metadata=metadata,
            ),
        )
    )
    events = [event async for event in port(client, input_request).stream(input_request)]
    result = events[-1].result
    assert result.response_kind == expected_kind
    assert result.gateway_error_code == code
    assert result.provider_status_code == 422
    assert result.tool_calls == ()
    assert result.failure_kind == {
        "MODEL_RESPONSE_INVALID": "invalid_response",
        "MODEL_REQUEST_INVALID": "client_error",
    }.get(code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "failure_kind"),
    [
        ("timeout", "request_timeout"),
        ("unavailable", "server_error"),
        ("transport", "network_error"),
        ("invalid_request", "client_error"),
        ("unknown", "unknown_error"),
        ("future_gateway_category", "unknown_error"),
    ],
)
async def test_studio_native_model_port_projects_safe_gateway_failure_kind(category, failure_kind):
    input_request = request(streaming=True)
    client = Client(
        events=(
            ModelStreamEvent(
                ModelStreamEventType.TURN_FAILED,
                "fixture",
                1,
                failure_category=category,
                gateway_error_code="MODEL_REQUEST_FAILED" if category == "invalid_request" else "",
                status_code=400 if category == "invalid_request" else None,
            ),
        )
    )
    events = [event async for event in port(client, input_request).stream(input_request)]
    assert events[-1].result.failure_kind == failure_kind
    assert events[-1].result.provider_failure_category == category
    if category == "invalid_request":
        assert events[-1].result.provider_status_code == 400
        assert events[-1].result.gateway_error_code == "MODEL_REQUEST_FAILED"
    assert events[-1].result.response_kind == (
        "provider_retryable_error"
        if category in {"timeout", "unavailable", "transport"}
        else "provider_terminal_error"
    )


@pytest.mark.asyncio
async def test_studio_native_model_port_streams_text_then_seals_tool_and_usage():
    input_request = request(streaming=True, tools=(TOOL,))
    client = Client(
        events=(
            ModelStreamEvent(ModelStreamEventType.TURN_STARTED, "fixture", 1),
            ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, "fixture", 2, text="Looking up"),
            ModelStreamEvent(
                ModelStreamEventType.TOOL_CALL_COMPLETED,
                "fixture",
                3,
                call_id="call-1",
                tool_name="read_value",
                arguments="{}",
            ),
            ModelStreamEvent(
                ModelStreamEventType.USAGE_UPDATED,
                "fixture",
                4,
                usage={"input_tokens": 12, "output_tokens": 4},
            ),
            ModelStreamEvent(
                ModelStreamEventType.TURN_COMPLETED,
                "fixture",
                5,
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    events = [event async for event in port(client, input_request).stream(input_request)]
    assert [(event.kind, event.seq) for event in events] == [
        ("text_delta", 1),
        ("final", 2),
    ]
    assert events[-1].result.response_kind == "tool_calls"
    assert events[-1].result.tool_calls[0].name == "read_value"
    assert events[-1].result.usage.input_tokens == 12
    assert client.calls[0][1]["retry_empty_stream"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_arguments", ['{"x":1,"x":2}', "null", "{"])
async def test_studio_native_stream_sealed_bad_arguments_keep_a_nonexecutable_call(
    raw_arguments: str,
):
    input_request = request(streaming=True, tools=(TOOL,))
    client = Client(
        events=(
            ModelStreamEvent(
                ModelStreamEventType.TOOL_CALL_COMPLETED,
                "fixture",
                1,
                call_id="call-1",
                tool_name="read_value",
                arguments=raw_arguments,
            ),
            ModelStreamEvent(
                ModelStreamEventType.TURN_COMPLETED,
                "fixture",
                2,
                metadata={"finish_reason": "tool_calls"},
            ),
        )
    )
    events = [event async for event in port(client, input_request).stream(input_request)]
    result = events[-1].result
    assert result.response_kind == "tool_calls"
    assert result.tool_calls[0].arguments == {}
    assert result.tool_calls[0].wire_arguments_invalid is True


@pytest.mark.asyncio
async def test_studio_native_model_port_stream_without_finish_is_retryable():
    input_request = request(streaming=True)
    client = Client(
        events=(
            ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, "fixture", 1, text="partial"),
            ModelStreamEvent(ModelStreamEventType.TURN_COMPLETED, "fixture", 2),
        )
    )
    events = [event async for event in port(client, input_request).stream(input_request)]
    assert events[-1].result.response_kind == "provider_retryable_error"
    assert events[-1].result.failure_kind == "incomplete_response"
    assert events[-1].result.text == "partial"


@pytest.mark.asyncio
async def test_studio_native_model_port_stream_eof_has_incomplete_response_cause():
    input_request = request(streaming=True)
    events = [event async for event in port(Client(), input_request).stream(input_request)]
    assert len(events) == 1
    assert events[0].result.response_kind == "provider_retryable_error"
    assert events[0].result.failure_kind == "incomplete_response"


@pytest.mark.asyncio
async def test_studio_native_model_port_rejects_foreign_scope_before_model_call():
    input_request = request()
    altered = input_request.model_copy(
        update={"scope": SCOPE.model_copy(update={"user_id": "other"})}
    )
    client = Client(response=response())
    with pytest.raises(ValueError, match="scope or profile"):
        await port(client, input_request).complete(altered)
    assert client.calls == []


@pytest.mark.asyncio
async def test_studio_native_model_port_binds_model_and_policy_from_build():
    input_request = request()
    client = Client(response=response())
    bound = StudioNativeModelPort.from_resolved_build(
        client,
        scope=SCOPE,
        resolved_agent_spec=resolved_build(),
        selected_model="fixture",
        profile_digest=input_request.profile.digest,
    )
    assert (await bound.complete(input_request)).response_kind == "final_candidate"
    assert client.calls[0][0] == MODEL
    assert client.calls[0][1]["network_policy"] == NETWORK


@pytest.mark.parametrize(
    ("build", "selected_model", "error"),
    [
        (resolved_build(agent_id="other"), "fixture", "Agent differs"),
        (resolved_build(), "missing", "missing or ambiguous"),
        (resolved_build(models=(MODEL, MODEL)), "fixture", "missing or ambiguous"),
    ],
)
def test_studio_native_model_port_rejects_invalid_build_model_binding(build, selected_model, error):
    input_request = request()
    with pytest.raises(ValueError, match=error):
        StudioNativeModelPort.from_resolved_build(
            Client(response=response()),
            scope=SCOPE,
            resolved_agent_spec=build,
            selected_model=selected_model,
            profile_digest=input_request.profile.digest,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("disclosure", [False, True])
async def test_explicit_public_summary_crosses_native_stream_without_private_reasoning(disclosure):
    client = Client(events=(
        ModelStreamEvent(ModelStreamEventType.PUBLIC_SUMMARY_DELTA, MODEL.model, 1,
                         text="Check the source first.\n"),
        ModelStreamEvent(ModelStreamEventType.REASONING_DELTA, MODEL.model, 2,
                         text="PRIVATE_CHAIN_FIXTURE"),
        ModelStreamEvent(ModelStreamEventType.TEXT_DELTA, MODEL.model, 3, text="Visible answer"),
        ModelStreamEvent(ModelStreamEventType.TURN_COMPLETED, MODEL.model, 4,
                         metadata={"finish_reason": "stop"}),
    ))
    req = request(streaming=True)
    public_model = MODEL.model_copy(update={"parameters": MODEL.parameters.model_copy(
        update={"public_thinking_summary": disclosure},
    )})
    events = [e async for e in port(client, req, model=public_model).stream(req)]
    assert [e.kind for e in events] == (["public_summary"] if disclosure else []) + ["text_delta", "final"]
    if disclosure:
        assert events[0].text == "Check the source first.\n"
    assert "PRIVATE_CHAIN_FIXTURE" not in "".join(e.model_dump_json() for e in events)
    assert events[-1].result.text == "Visible answer"
