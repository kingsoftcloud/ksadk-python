"""Studio 绑定的 Harness Reasoner 用量映射契约。"""

from __future__ import annotations

import pytest

from ksadk.studio.contracts import NetworkPolicy, ResolvedModel, ModelParameters
from ksadk.studio.model_client import ModelResponse, ToolCall, Usage
from ksadk.studio.plugin_runtime import _StudioHarnessReasoner


def _model() -> ResolvedModel:
    return ResolvedModel(
        provider="openai",
        model="fixture-model",
        endpoint_url="https://model.example.test/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
        parameters=ModelParameters(),
    )


class _FakeClient:
    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append(kwargs)
        return self._response


def _reasoner(client: _FakeClient) -> _StudioHarnessReasoner:
    return _StudioHarnessReasoner(
        client,  # type: ignore[arg-type]
        model=_model(),
        network_policy=NetworkPolicy(allowed_hosts=["model.example.test"]),
        timeout_seconds=30,
        max_attempts=1,
        backoff_seconds=0,
    )


def _response(usage: Usage, content: str = "ok") -> ModelResponse:
    return ModelResponse(
        content=content,
        finish_reason="stop",
        usage=usage,
        tool_calls=[ToolCall(id="call-1", name="t", arguments="{}")],
        raw_message={},
    )


@pytest.mark.asyncio
async def test_reasoner_maps_model_usage_into_turn():
    client = _FakeClient(
        _response(
            Usage(
                input_tokens=120,
                output_tokens=45,
                total_tokens=165,
                cached_input_tokens=32,
                reasoning_output_tokens=18,
                reported=True,
                source="model-provider",
            )
        )
    )
    reasoner = _reasoner(client)
    turn = await reasoner.complete(model="fixture-model", prompt="", messages=[], tools=[])
    assert turn.usage == {
        "input_tokens": 120,
        "output_tokens": 45,
        "cached_tokens": 32,
        "reasoning_tokens": 18,
    }


@pytest.mark.asyncio
async def test_reasoner_keeps_usage_absent_when_provider_did_not_report():
    client = _FakeClient(_response(Usage(reported=False)))
    reasoner = _reasoner(client)
    turn = await reasoner.complete(model="fixture-model", prompt="", messages=[], tools=[])
    assert not turn.usage


@pytest.mark.asyncio
async def test_reasoner_maps_tool_calls_and_final_text():
    client = _FakeClient(_response(Usage(reported=False), content="done"))
    reasoner = _reasoner(client)
    turn = await reasoner.complete(model="fixture-model", prompt="", messages=[], tools=[])
    assert turn.final_text == "done"
    assert turn.tool_calls[0].name == "t"
    assert turn.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_reasoner_passes_reasoning_through():
    client = _FakeClient(
        ModelResponse(
            content="答案",
            finish_reason="stop",
            usage=Usage(reported=False),
            tool_calls=[],
            raw_message={},
            reasoning="先算 417*29。",
        )
    )
    reasoner = _reasoner(client)
    turn = await reasoner.complete(model="fixture-model", prompt="", messages=[], tools=[])
    assert turn.reasoning == "先算 417*29。"


@pytest.mark.asyncio
async def test_reasoner_reasoning_absent_stays_none():
    client = _FakeClient(_response(Usage(reported=False), content="答案"))
    reasoner = _reasoner(client)
    turn = await reasoner.complete(model="fixture-model", prompt="", messages=[], tools=[])
    assert turn.reasoning is None
