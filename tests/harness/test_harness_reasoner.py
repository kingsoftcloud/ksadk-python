from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ksadk.harness import HarnessApp, HarnessConfig
from ksadk.harness.config import McpToolSpec
from ksadk.harness.reasoner import (
    HarnessReasoningTurn,
    LiteLLMHarnessReasoner,
    resolve_model_identifier,
)
from ksadk.harness.runtime import HarnessRuntimeAdapter
from ksadk.runtime import StartRequest


@pytest.mark.asyncio
async def test_native_harness_consumes_studio_conversation_history(tmp_path):
    captured: list[dict] = []

    class CaptureReasoner:
        async def complete(self, **kwargs):
            captured.extend(kwargs["messages"])
            return HarnessReasoningTurn(final_text="ok")

    adapter = HarnessRuntimeAdapter(
        HarnessConfig(model="glm-5.2", prompt="budget assistant"),
        reasoner=CaptureReasoner(),
        workspace_root=tmp_path,
    )
    result = await adapter.execute_request(
        StartRequest(
            input="compress the prior result",
            user_id="u",
            session_id="s",
            metadata={
                "conversation_request": {
                    "messages": [
                        {"role": "user", "content": "budget is 50"},
                        {"role": "assistant", "content": "total is 55"},
                        {"role": "user", "content": "compress the prior result"},
                    ]
                }
            },
        )
    )

    assert result["output"] == "ok"
    assert [item["role"] for item in captured] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert captured[-1]["content"] == "compress the prior result"


@pytest.mark.asyncio
async def test_native_harness_reports_aggregated_model_usage(tmp_path):
    class UsageReasoner:
        async def complete(self, **_kwargs):
            return HarnessReasoningTurn(
                final_text="done",
                usage={
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cached_tokens": 20,
                    "reasoning_tokens": 10,
                },
            )

    adapter = HarnessRuntimeAdapter(
        HarnessConfig(model="glm-5.2", prompt="budget assistant"),
        reasoner=UsageReasoner(),
        workspace_root=tmp_path,
    )
    handle = await adapter.start(
        StartRequest(input="calculate", user_id="u", session_id="s")
    )
    events = [event async for event in adapter.stream(handle)]
    usage = next(event for event in events if event.event_type == "usage.reported")

    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.total_tokens == 150
    assert usage.cached_tokens == 20
    assert usage.reasoning_tokens == 10


@pytest.mark.asyncio
async def test_production_reasoner_uses_model_tool_loop_without_echo(monkeypatch, tmp_path):
    import litellm

    (tmp_path / "facts.txt").write_text("provider-tool-result", encoding="utf-8")
    requests = []

    async def fake_acompletion(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            tool_call = SimpleNamespace(
                id="read-1",
                function=SimpleNamespace(
                    name="sandbox_read_file",
                    arguments=json.dumps({"path": "facts.txt"}),
                ),
            )
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        else:
            assert kwargs["messages"][-1]["role"] == "tool"
            assert "provider-tool-result" in kwargs["messages"][-1]["content"]
            message = SimpleNamespace(content="grounded final answer", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    harness = HarnessApp(
        HarnessConfig(model="glm-5.2", prompt="read before answering"),
        workspace_root=tmp_path,
    )
    adapter = harness.adapter()
    assert isinstance(adapter, HarnessRuntimeAdapter)
    result = await adapter.execute_request(
        StartRequest(input="what is in facts?", user_id="u", session_id="s")
    )

    assert result["output"] == "grounded final answer"
    assert result["output"] != "what is in facts?"
    assert [item["name"] for item in result["tool_calls"]] == ["sandbox_read_file"]
    assert requests[0]["model"] == "openai/glm-5.2"
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "sandbox_read_file",
        "sandbox_run_command",
    ]
    raw_arguments = requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(raw_arguments) == {"path": "facts.txt"}
    assert result["model"] == "glm-5.2"
    assert result["prompt"] == "read before answering"


def test_model_profile_reference_resolves_without_embedding_credentials(monkeypatch):
    ref = "model-profile://finance-model@1.2.0"
    monkeypatch.setenv("KSADK_MODEL_PROFILE_MAP", json.dumps({ref: "provider/finance-v3"}))
    assert resolve_model_identifier(ref) == "provider/finance-v3"


def test_model_profile_reference_falls_back_to_openai_compatible_name(monkeypatch):
    monkeypatch.delenv("KSADK_MODEL_PROFILE_MAP", raising=False)
    assert resolve_model_identifier("model-profile://glm-5.3@live") == "openai/glm-5.3"


@pytest.mark.asyncio
async def test_reasoner_forwards_preflight_output_budget_to_provider(monkeypatch):
    import litellm

    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    await LiteLLMHarnessReasoner(streaming=False).complete(
        model="glm-5.3",
        prompt="p",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
        max_output_tokens=17,
    )
    assert captured["max_tokens"] == 17


def test_invalid_model_profile_map_fails_honestly(monkeypatch):
    monkeypatch.setenv("KSADK_MODEL_PROFILE_MAP", "not-json")
    with pytest.raises(RuntimeError, match="valid JSON"):
        resolve_model_identifier("model-profile://glm-5.3@live")


@pytest.mark.asyncio
async def test_production_reasoner_reassembles_streaming_text_and_usage(monkeypatch):
    import litellm

    async def chunks():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="stream ", tool_calls=[]))],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=2),
        )

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        return chunks()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    turn = await LiteLLMHarnessReasoner(streaming=True).complete(
        model="glm-5.3",
        prompt="",
        messages=({"role": "user", "content": "x"},),
        tools=(),
    )
    assert turn.final_text == "stream ok"
    assert turn.usage == {"input_tokens": 9, "output_tokens": 2}


@pytest.mark.asyncio
async def test_production_reasoner_reassembles_fragmented_streaming_tool_call(monkeypatch):
    import litellm

    async def chunks():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-1",
                                function=SimpleNamespace(
                                    name="sandbox_read_",
                                    arguments='{"path":"facts',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name="file",
                                    arguments='.txt"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )

    async def fake_acompletion(**kwargs):
        assert kwargs["stream"] is True
        return chunks()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    turn = await LiteLLMHarnessReasoner(streaming=True).complete(
        model="glm-5.3",
        prompt="",
        messages=({"role": "user", "content": "x"},),
        tools=(),
    )
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].call_id == "call-1"
    assert turn.tool_calls[0].name == "sandbox_read_file"
    assert turn.tool_calls[0].arguments == {"path": "facts.txt"}


@pytest.mark.asyncio
async def test_streaming_reasoner_rejects_conflicting_call_ids_for_one_index():
    async def chunks():
        for call_id, arguments in (("call-1", '{"path":"'), ("call-2", 'x"}')):
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=call_id,
                                    function=SimpleNamespace(
                                        name="sandbox_read_file" if call_id == "call-1" else None,
                                        arguments=arguments,
                                    ),
                                )
                            ],
                        )
                    )
                ],
                usage=None,
            )

    with pytest.raises(RuntimeError, match="conflicting tool call id"):
        await LiteLLMHarnessReasoner._consume_stream(chunks(), model="glm-5.3")


@pytest.mark.asyncio
async def test_streaming_reasoner_rejects_tool_call_without_provider_identity():
    async def chunks():
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name="sandbox_read_file",
                                    arguments='{"path":"x"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )

    with pytest.raises(RuntimeError, match="missing tool call id"):
        await LiteLLMHarnessReasoner._consume_stream(chunks(), model="glm-5.3")


@pytest.mark.asyncio
async def test_mcp_tool_adapter_uses_public_tool_api(monkeypatch):
    """MCP wrappers use raw schema + run_async, never ADK private methods."""
    from types import SimpleNamespace

    from ksadk.harness.tools import load_mcp_tools

    class _NativeTool:
        name = "public_lookup"
        description = "Public lookup"
        raw_mcp_tool = SimpleNamespace(
            inputSchema={"type": "object", "properties": {"value": {"type": "string"}}}
        )

        async def run_async(self, *, args, tool_context):
            assert tool_context.__class__.__name__ in {"Context", "ToolContext"}
            if args["value"] == "confirm":
                tool_context.request_confirmation(hint="approve lookup")
                return {"error": "confirmation needed"}
            return {"content": [{"text": f"ok:{args['value']}"}]}

    class _Toolset:
        async def get_tools_with_prefix(self):
            return [_NativeTool()]

        async def close(self):
            return None

    monkeypatch.setattr("ksadk.harness.tools.build_mcp_toolset", lambda _config: _Toolset())
    _, tools = await load_mcp_tools(
        McpToolSpec(name="fixture", url="http://fixture/mcp", tool_filter=("public_lookup",))
    )
    assert await tools[0].call({"value": "x"}) == {"content": [{"text": "ok:x"}]}
    confirmation = await tools[0].call({"value": "confirm"})
    assert confirmation == {
        "ok": False,
        "confirmation_required": True,
        "confirmation_ids": ["public_lookup"],
        "error": "confirmation needed",
    }
