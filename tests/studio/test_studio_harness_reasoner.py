"""Studio 绑定的 Harness Reasoner 用量映射契约。"""

from __future__ import annotations

import pytest

from ksadk.studio.contracts import ModelParameters, NetworkPolicy, ResolvedModel
from ksadk.studio.model_client import ModelResponse, ModelStreamChunk, ToolCall, Usage
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


class _MalformedStreamClient(_FakeClient):
    async def stream(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append(kwargs)
        yield ModelStreamChunk(
            text="三份资料已经齐备，接下来保存报告。",
            tool_calls=(
                ToolCall(
                    id="bad-call",
                    name="write_workspace_file",
                    arguments='{"path":"report.md","content":"truncated',
                ),
            ),
            done=True,
        )


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


@pytest.mark.asyncio
async def test_stream_reasoner_repairs_malformed_native_tool_arguments_once():
    client = _MalformedStreamClient(
        ModelResponse(
            content="",
            finish_reason="tool_calls",
            usage=Usage(reported=False),
            tool_calls=[
                ToolCall(
                    id="repaired-call",
                    name="write_workspace_file",
                    arguments='{"path":"report.md","content":"# Report"}',
                )
            ],
            raw_message={},
        )
    )
    reasoner = _reasoner(client)
    tools = [
        type(
            "Tool",
            (),
            {
                "openai_schema": {
                    "type": "function",
                    "function": {
                        "name": "write_workspace_file",
                        "parameters": {"type": "object"},
                    },
                }
            },
        )()
    ]

    chunks = [
        chunk
        async for chunk in reasoner.stream_complete(
            model="fixture-model",
            prompt="",
            messages=[{"role": "user", "content": "write report"}],
            tools=tools,
            max_output_tokens=4096,
        )
    ]

    turn = chunks[-1]["turn"]
    assert turn.final_text == "三份资料已经齐备，接下来保存报告。"
    assert turn.tool_calls[0].call_id == "repaired-call"
    assert turn.tool_calls[0].arguments == {
        "path": "report.md",
        "content": "# Report",
    }
    repair_call = client.calls[-1]
    assert repair_call["max_output_tokens"] == 16_384
    assert repair_call["retry_on_length"] is True
    assert "truncated" not in str(repair_call["messages"])
