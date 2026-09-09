from __future__ import annotations

import pytest

from ksadk.events.canonical import ItemCompleted, RunCompleted
from ksadk.events.content import TextContent
from ksadk.harness.config import HarnessConfig, McpToolSpec
from ksadk.harness.engine.mcp_disclosure import (
    MCP_CALL_TOOL_TOOL,
    MCP_LIST_TOOLS_TOOL,
    MCP_READ_SCHEMA_TOOL,
)
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.plugins.providers.harness import HarnessSkillContribution
from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter
from ksadk.runtime import StartRequest
from tests.harness.fixtures.mcp_server import run_fixture_mcp_server


@pytest.mark.asyncio
async def test_provider_resolves_only_locked_model_reference():
    from ksadk.plugins.providers.harness_managed import _BoundModelReasoner

    class Reasoner:
        async def complete(self, *, model, prompt, messages, tools):
            assert model == "glm-5.3"
            return HarnessReasoningTurn(final_text="ok")

    bound = _BoundModelReasoner(Reasoner(), "model-profile://glm-5.3@1", "glm-5.3")
    result = await bound.complete(
        model="model-profile://glm-5.3@1", prompt="", messages=[], tools=[]
    )
    assert result.final_text == "ok"
    with pytest.raises(ValueError, match="Unbound model"):
        await bound.complete(model="model-profile://other@1", prompt="", messages=[], tools=[])


def test_provider_skill_resource_stays_inside_locked_package(tmp_path):
    from ksadk.plugins.providers.harness_managed import _ProviderSkillSource

    root = tmp_path / "skill"
    root.mkdir()
    (root / "policy.txt").write_text("HBP-test", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    source = _ProviderSkillSource(
        [
            HarnessSkillContribution(
                name="budget",
                instructions="Read policy.txt",
                resource_root=root,
            )
        ]
    )
    ref = source.refs[0]
    assert source.resource(ref, "policy.txt") == b"HBP-test"
    for path in ("../outside.txt", "escape.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(ValueError, match="escapes"):
            source.resource(ref, path)


class _ManagedMcpReasoner:
    def __init__(self) -> None:
        self.turn = 0
        self.first_tools: set[str] = set()
        self.first_messages: tuple[dict, ...] = ()

    async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
        del model, prompt, max_output_tokens
        self.turn += 1
        if self.turn == 1:
            self.first_tools = {tool.name for tool in tools}
            self.first_messages = tuple(messages)
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="list-1",
                        name=MCP_LIST_TOOLS_TOOL,
                        arguments={"server_id": "mcp://fixture@1.0.0"},
                    ),
                )
            )
        if self.turn == 2:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="schema-1",
                        name=MCP_READ_SCHEMA_TOOL,
                        arguments={
                            "server_id": "mcp://fixture@1.0.0",
                            "tool_name": "lookup",
                        },
                    ),
                )
            )
        if self.turn == 3:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="call-1",
                        name=MCP_CALL_TOOL_TOOL,
                        arguments={
                            "server_id": "mcp://fixture@1.0.0",
                            "tool_name": "lookup",
                            "arguments": {"value": "ALPHA"},
                        },
                    ),
                )
            )
        return HarnessReasoningTurn(
            final_text="managed provider completed",
            usage={"input_tokens": 20, "output_tokens": 4},
        )


@pytest.mark.asyncio
async def test_dsh_contributions_run_through_managed_harness(tmp_path):
    reasoner = _ManagedMcpReasoner()
    with run_fixture_mcp_server(label="managed") as fixture:
        adapter = await build_managed_provider_adapter(
            HarnessConfig(
                model="fixture-model",
                prompt="Use bound capabilities.",
                mcp_tools=(McpToolSpec(name="fixture", url=fixture.url),),
            ),
            agent_name="managed-provider-agent",
            workspace_root=tmp_path,
            reasoner=reasoner,
            skills=(
                HarnessSkillContribution(
                    name="managed-style",
                    instructions="Answer with a concise managed-runtime result.",
                ),
            ),
        )

        assert isinstance(adapter, ManagedHarnessRuntimeAdapter)
        assert adapter.runtime.native_capabilities()["progressive_disclosure"] == {
            "skill": True,
            "mcp": True,
        }
        assert [
            binding.capability_ref for binding in adapter.harness_spec.capabilities.mcp_bindings
        ] == ["mcp://fixture@1.0.0"]
        assert [
            binding.capability_ref for binding in adapter.harness_spec.capabilities.skill_bindings
        ] == ["skill://managed-style@1.0.0"]

        handle = await adapter.start(
            StartRequest(
                input="lookup ALPHA",
                user_id="user",
                session_id="session",
                agent_id="agent",
                metadata={"invocation_id": "managed-provider-run"},
            )
        )
        events = [event async for event in adapter.stream(handle)]

        assert MCP_LIST_TOOLS_TOOL in reasoner.first_tools
        assert MCP_READ_SCHEMA_TOOL in reasoner.first_tools
        assert MCP_CALL_TOOL_TOOL in reasoner.first_tools
        assert "lookup" not in reasoner.first_tools
        assert any(
            "managed-style" in str(message.get("content")) for message in reasoner.first_messages
        )
        assert any(isinstance(event, RunCompleted) for event in events)
        final = next(
            event
            for event in events
            if isinstance(event, ItemCompleted) and event.item_kind == "message"
        )
        assert isinstance(final.snapshot.parts[0], TextContent)
        assert final.snapshot.parts[0].text == "managed provider completed"

        await adapter.close_all()
