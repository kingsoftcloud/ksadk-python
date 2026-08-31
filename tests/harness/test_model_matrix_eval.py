from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ksadk.harness.model_matrix_eval import (
    DiscoveredModel,
    ModelMatrixRequirements,
    OpenAICompatibleOverflowProbe,
    OpenAICompatibleStreamingProbe,
    discover_models,
    evaluate_model_matrix,
    select_models,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall


def test_discover_models_projects_gateway_metadata_without_credentials() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "glm-5.3",
                        "owned_by": "provider-a",
                        "architecture": {"input_modalities": ["text", "image"]},
                        "context_window": "200000",
                    },
                    {"id": "kimi-k3", "context_length": 131072},
                    {"object": "model"},
                ]
            },
        )

    models = discover_models(
        base_url="https://models.example/v1/",
        api_key="secret-value",
        transport=httpx.MockTransport(handler),
    )
    assert [item.model_id for item in models] == ["glm-5.3", "kimi-k3"]
    assert models[0].input_modalities == ("text", "image")
    assert models[0].context_window == 200000
    assert observed["authorization"] == "Bearer secret-value"
    assert "secret-value" not in json.dumps([item.__dict__ for item in models])


def test_select_models_is_explicit_and_rejects_gateway_drift() -> None:
    models = (DiscoveredModel("a"), DiscoveredModel("b"), DiscoveredModel("c"))
    assert [item.model_id for item in select_models(models, ("c", "a"))] == ["c", "a"]
    assert [item.model_id for item in select_models(models, (), limit=2)] == ["a", "b"]
    with pytest.raises(ValueError, match="not returned"):
        select_models(models, ("missing",))


def test_evaluate_model_matrix_checks_chat_tool_calling_and_usage() -> None:
    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(
                final_text="MODEL_MATRIX_OK",
                usage={"input_tokens": 8, "output_tokens": 2},
            )

    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("model-a"), DiscoveredModel("model-b")),
            reasoner_factory=Reasoner,
        )
    )
    assert report["all_passed"] is True
    assert report["passed_count"] == 2
    assert all(item["usage_reported"] for item in report["results"])
    assert report["schemaVersion"] == 1
    assert report["status"] == "ready"
    assert report["summary"] == {"passed": 2, "failed": 0, "skipped": 0}


def test_declared_capability_requirements_never_guess_missing_metadata() -> None:
    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="MODEL_MATRIX_OK")

    report = asyncio.run(
        evaluate_model_matrix(
            (
                DiscoveredModel(
                    "known", input_modalities=("text", "image"), context_window=200000
                ),
                DiscoveredModel("metadata-missing"),
            ),
            reasoner_factory=Reasoner,
            requirements=ModelMatrixRequirements(
                min_context_window=131072,
                required_input_modalities=("text", "image"),
            ),
        )
    )

    assert report["status"] == "blocked"
    assert report["passed_count"] == 1
    missing = report["results"][1]
    assert "required_context_window_unavailable" in missing["errors"]
    assert "required_input_modality_unavailable" in missing["errors"]
    assert missing["capability_findings"][0]["reason"] == "metadata_missing"


def test_required_streaming_without_probe_is_explicit_blocker() -> None:
    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="MODEL_MATRIX_OK")

    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("model-a"),),
            reasoner_factory=Reasoner,
            requirements=ModelMatrixRequirements(require_streaming=True),
        )
    )

    assert report["status"] == "blocked"
    assert report["results"][0]["errors"][-1] == "streaming_probe_not_configured"


def test_evaluate_model_matrix_reports_incompatible_model_without_aborting_matrix() -> None:
    fake_key = "sk-" + "matrix-secret-value"

    class Reasoner:
        async def complete(self, *, model, tools, **_):
            if model == "broken":
                raise RuntimeError(f"provider unavailable api_key={fake_key}")
            if tools:
                return HarnessReasoningTurn()
            return HarnessReasoningTurn(final_text="MODEL_MATRIX_OK")

    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("broken"), DiscoveredModel("text-only")),
            reasoner_factory=Reasoner,
        )
    )
    assert report["all_passed"] is False
    assert report["passed_count"] == 0
    assert report["results"][0]["errors"][0].startswith("basic_chat:RuntimeError")
    assert fake_key not in json.dumps(report)
    assert "expected_tool_call_missing" in report["results"][1]["errors"]


def test_streaming_probe_reassembles_text_and_fragmented_tool_arguments() -> None:
    responses = iter(
        (
            (
                'data: {"choices":[{"delta":{"content":"STREAM_"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"content":"MATRIX_OK"},'
                '"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            ),
            (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"id":"call-1","function":{"name":"record_model_",'
                '"arguments":"{\\"code\\":\\"KS"}}]},"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"name":"matrix_probe",'
                '"arguments":"ADK-42\\"}"}}]},"finish_reason":"tool_calls"}],'
                '"usage":{"prompt_tokens":11,"completion_tokens":5}}\n\n'
                "data: [DONE]\n\n"
            ),
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=next(responses),
        )

    probe = OpenAICompatibleStreamingProbe(
        base_url="https://models.example/v1",
        api_key="test-secret",
        transport=httpx.MockTransport(handler),
    )

    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="MODEL_MATRIX_OK")

    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("model-a"),),
            reasoner_factory=Reasoner,
            streaming_probe=probe,
        )
    )
    result = report["results"][0]
    assert report["all_passed"] is True
    assert result["stream_text"] is True
    assert result["stream_text_details"]["chunk_count"] == 2
    assert result["stream_tool_calling"] is True
    assert result["stream_tool_details"]["fragment_count"] == 2
    assert result["stream_tool_details"]["finish_reason"] == "tool_calls"


def test_streaming_probe_reports_malformed_tool_json_without_leaking_credentials() -> None:
    fake_key = "sk-" + "stream-secret-value"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"name":"record_model_matrix_probe",'
                '"arguments":"{bad"}}]},"finish_reason":"tool_calls"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    probe = OpenAICompatibleStreamingProbe(
        base_url="https://models.example/v1",
        api_key=fake_key,
        transport=httpx.MockTransport(handler),
    )
    tool = type(
        "Tool",
        (),
        {
            "name": "record_model_matrix_probe",
            "openai_schema": {"type": "function", "function": {"name": "x"}},
        },
    )()
    with pytest.raises(RuntimeError, match="invalid JSON tool arguments") as caught:
        asyncio.run(probe.probe_tool(model="model-a", tool=tool))  # type: ignore[arg-type]
    assert fake_key not in str(caught.value)


def test_overflow_probe_requires_classified_context_length_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "This model's maximum context length is 8192 tokens"}},
        )

    probe = OpenAICompatibleOverflowProbe(
        base_url="http://gateway.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(probe.probe_overflow(model="model-a", context_window=8192))
    assert result["passed"] is True
    assert result["failure_kind"] == "context_length"
    assert result["status_code"] == 400
    assert "test-key" not in str(result)


def test_overflow_probe_fails_when_provider_accepts_oversized_prompt() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    probe = OpenAICompatibleOverflowProbe(
        base_url="http://gateway.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(probe.probe_overflow(model="model-a", context_window=8192))
    assert result["passed"] is False
    assert result["overflow_detected"] is False


def test_overflow_probe_fails_on_unclassified_generic_500() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    probe = OpenAICompatibleOverflowProbe(
        base_url="http://gateway.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(probe.probe_overflow(model="model-a", context_window=None))
    assert result["passed"] is False
    assert result["overflow_detected"] is True
    assert result["failure_kind"] != "context_length"


def test_evaluate_model_matrix_blocks_unclassified_overflow_and_missing_usage() -> None:
    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="MODEL_MATRIX_OK", usage=None)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    probe = OpenAICompatibleOverflowProbe(
        base_url="http://gateway.test/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("model-a"),),
            reasoner_factory=Reasoner,
            overflow_probe=probe,
            requirements=ModelMatrixRequirements(require_usage=True),
        )
    )
    assert report["all_passed"] is False
    result = report["results"][0]
    assert result["overflow_classified"] is False
    assert "overflow_failure_not_classified" in result["errors"]
    assert "usage_not_reported" in result["errors"]


def test_required_overflow_without_probe_is_explicit_blocker() -> None:
    class Reasoner:
        async def complete(self, *, tools, **_):
            if tools:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="call-1",
                            name="record_model_matrix_probe",
                            arguments={"code": "KSADK-42"},
                        ),
                    )
                )
            return HarnessReasoningTurn(
                final_text="MODEL_MATRIX_OK",
                usage={"input_tokens": 8, "output_tokens": 2},
            )

    report = asyncio.run(
        evaluate_model_matrix(
            (DiscoveredModel("model-a"),),
            reasoner_factory=Reasoner,
            requirements=ModelMatrixRequirements(require_overflow=True),
        )
    )
    assert report["all_passed"] is False
    assert "overflow_probe_not_configured" in report["results"][0]["errors"]
