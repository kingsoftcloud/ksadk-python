from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ksadk.harness import HarnessApp, HarnessConfig
from ksadk.harness.config import McpToolSpec


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
    result = await harness.build_runner().invoke({"input": "what is in facts?"})

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
