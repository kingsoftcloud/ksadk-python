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
async def test_dynamic_child_timeout_cannot_exceed_agent_execution_timeout(tmp_path):
    adapter = await build_managed_provider_adapter(
        HarnessConfig(model="fixture", prompt="test"),
        agent_name="bounded-child-agent",
        workspace_root=tmp_path,
        reasoner=_ManagedMcpReasoner(),
        tool_contracts={
            "execution": {
                "timeoutSeconds": 120,
                "childTimeoutSeconds": 300,
                "childMaxTotalTokens": 1536,
            }
        },
    )

    assert adapter._engine._delegation_runtime._child_timeout_seconds == 120
    assert adapter._engine._delegation_runtime._child_max_total_tokens == 1536
    assert "只在对话中展示同格式文本不算完成" in adapter.harness_spec.prompt.instructions
    await adapter.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,written", [("approve", True), ("reject", False)])
async def test_builtin_write_runs_only_after_studio_approval(tmp_path, decision, written):
    import hashlib

    from ksadk.events.canonical import InteractionRequested
    from ksadk.runtime import ResumePayload, ResumeTarget

    class Reasoner:
        turn = 0

        async def complete(self, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return HarnessReasoningTurn(tool_calls=(HarnessToolCall(
                    call_id="write-test", name="write_workspace_file",
                    arguments={"path": "approval.txt", "content": "verified"},
                ),))
            if self.turn == 2 and written:
                return HarnessReasoningTurn(tool_calls=(HarnessToolCall(
                    call_id="write-second", name="write_workspace_file",
                    arguments={"path": "approval.txt", "content": "verified twice"},
                ),))
            return HarnessReasoningTurn(final_text="finished")

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    state_dir = tmp_path / "state"
    adapter = await build_managed_provider_adapter(
        HarnessConfig(model="fixture", prompt="test"), agent_name="test",
        workspace_root=bundle, bundle_root=bundle, state_dir=state_dir,
        reasoner=Reasoner(), tool_contracts={
            "capabilities": {"tools": [{
                "name": "write_workspace_file", "executor": "builtin",
                "approval": "never", "sideEffect": "none",
            }]},
        },
    )
    handle = await adapter.start(StartRequest(
        input="write", user_id="user", agent_id="agent", session_id="session",
    ))
    events = [event async for event in adapter.stream(handle)]
    assert any(isinstance(event, InteractionRequested) for event in events)
    first_approval = next(event for event in events if isinstance(event, InteractionRequested))
    target = (state_dir / "tool-workspaces" / hashlib.sha256(b"test").hexdigest()
              / ".harness-tools/workspace/approval.txt")
    assert not target.exists()
    handle = await adapter.resume(
        handle, ResumeTarget(kind="checkpoint_id", id="test"),
        ResumePayload(kind="approval_decision", call_id="write-test", data={"decision": decision}),
    )
    events = [event async for event in adapter.stream(handle)]
    if written:
        second_approval = next(event for event in events if isinstance(event, InteractionRequested))
        assert first_approval.interaction_id != second_approval.interaction_id
        assert target.read_text() == "verified"
        handle = await adapter.resume(
            handle, ResumeTarget(kind="checkpoint_id", id="test"),
            ResumePayload(kind="approval_decision", call_id="write-second",
                          data={"decision": "approve"}),
        )
        events = [event async for event in adapter.stream(handle)]
    assert any(isinstance(event, RunCompleted) for event in events)
    assert target.exists() is written
    if written:
        assert target.read_text() == "verified twice"
    assert not list(bundle.iterdir()), "builtin execution must never mutate the locked Bundle"
    await adapter.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_label,offloaded", [
    ("managed", False), ("large-result-" * 600, True), ("phone:13800138000", True),
], ids=["small", "large", "sensitive"])
async def test_dsh_contributions_run_through_managed_harness(tmp_path, result_label, offloaded):
    reasoner = _ManagedMcpReasoner()
    with run_fixture_mcp_server(label=result_label) as fixture:
        adapter = await build_managed_provider_adapter(
            HarnessConfig(
                model="fixture-model",
                prompt="Use bound capabilities.",
                mcp_tools=(McpToolSpec(name="fixture", url=fixture.url, api_key="harness-secret"),),
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
        completed_run = next(event for event in events if isinstance(event, RunCompleted))
        final_item_id = completed_run.output_refs[0].item_id
        final = next(
            event
            for event in events
            if isinstance(event, ItemCompleted)
            and event.item_kind == "message"
            and event.item_id == final_item_id
        )
        assert isinstance(final.snapshot.parts[0], TextContent)
        assert final.snapshot.parts[0].text == "managed provider completed"
        assert fixture.log.calls == [("lookup", "ALPHA")]

        store = adapter._provider_artifact_store
        rows = store._db.execute("SELECT run_id, name FROM artifacts").fetchall()
        assert bool(rows) is offloaded
        if offloaded:
            record = store.latest(*rows[0])
            assert record is not None and record.bytes > 0
            assert record.uri.startswith("artifact://")

        await adapter.close_all()
