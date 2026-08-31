from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ksadk.harness.model_matrix_eval import (
    DiscoveredModel,
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
