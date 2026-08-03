from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import builtin_tool_contracts
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilitiesSpec,
    CapabilityRef,
    CompactionSpec,
    ContextSpec,
    ExecutionSpec,
    Instructions,
    MCPServerRef,
    ModelParameters,
    ModelSpec,
    NetworkPolicy,
    SecuritySpec,
    ToolContract,
    Usage,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.model_client import ModelResponse, ToolCall
from ksadk.studio.runtime import ContextManager, LocalAgentRuntime
from ksadk.studio.workspace import Workspace


class FakeModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, model, **kwargs):
        self.calls.append((model, kwargs))
        return self.responses.pop(0)


def _workspace_and_build(
    tmp_path: Path,
    *,
    tools=None,
    skills=None,
    mcp_servers=None,
    strategy: str = "direct",
    allowed_permissions=None,
    context=None,
):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    draft = AgentDraft(
        metadata=AgentMetadata(id="demo-agent", name="Demo"),
        spec=AgentSpec(
            instructions=Instructions(system="Only answer the user."),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
                parameters=ModelParameters(max_tokens=128),
            ),
            capabilities=CapabilitiesSpec(
                tools=tools or [],
                skills=skills or [],
                mcp_servers=mcp_servers or [],
            ),
            execution=ExecutionSpec(strategy=strategy),
            context=context or ContextSpec(),
            security=SecuritySpec(
                allowed_permissions=allowed_permissions or ["system:time:read"],
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )
    build = AgentBundleBuilder(workspace).build(draft)
    return workspace, build


@pytest.mark.asyncio
async def test_runtime_loads_bundle_and_persists_events(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    client = FakeModelClient(
        [
            ModelResponse(
                content="AGENTKIT_OK",
                finish_reason="stop",
                usage=Usage(input_tokens=5, output_tokens=2, total_tokens=7),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "AGENTKIT_OK"},
            )
        ]
    )
    runtime = LocalAgentRuntime(workspace, model_client=client)

    run = await runtime.run(build.id, "say ok", session_id="ses_demo")

    assert run.status == "COMPLETED"
    assert run.output == "AGENTKIT_OK"
    assert run.usage.total_tokens == 7
    events = runtime.event_store.events(run.id)
    assert [event.id for event in events] == list(range(1, len(events) + 1))
    assert {event.type for event in events} >= {
        "run.created",
        "run.started",
        "model.requested",
        "model.completed",
        "message.delta",
        "run.completed",
    }
    trace = runtime.event_store.trace(run.trace_id)
    assert trace["run"]["id"] == run.id
    assert client.calls[0][1]["messages"][-1]["content"] == "say ok"


@pytest.mark.asyncio
async def test_runtime_executes_declared_builtin_tool(tmp_path: Path):
    echo = ToolContract(
        name="builtin.echo",
        version="1.0.0",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )
    workspace, build = _workspace_and_build(tmp_path, tools=[echo])
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "builtin.echo", "arguments": '{"value":"hello"}'},
    }
    client = FakeModelClient(
        [
            ModelResponse(
                content="",
                finish_reason="tool_calls",
                usage=Usage(total_tokens=2),
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="builtin.echo",
                        arguments='{"value":"hello"}',
                        raw=tool_call,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
            ),
            ModelResponse(
                content="hello",
                finish_reason="stop",
                usage=Usage(total_tokens=2),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "hello"},
            ),
        ]
    )
    runtime = LocalAgentRuntime(workspace, model_client=client)

    run = await runtime.run(build.id, "echo hello")

    assert run.status == "COMPLETED"
    assert run.output == "hello"
    assert json.loads(client.calls[1][1]["messages"][-1]["content"]) == {"value": "hello"}
    assert {event.type for event in runtime.event_store.events(run.id)} >= {
        "tool.requested",
        "tool.completed",
    }


@pytest.mark.asyncio
async def test_runtime_maps_safe_model_failure_without_secret(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)

    class FailingClient:
        async def complete(self, *_args, **_kwargs):
            from ksadk.studio.errors import StudioError

            raise StudioError(
                "MODEL_REQUEST_FAILED",
                "模型服务返回错误",
                status_code=502,
            )

    runtime = LocalAgentRuntime(workspace, model_client=FailingClient())
    run = await runtime.run(build.id, "test")

    assert run.status == "FAILED"
    assert run.error == {
        "code": "MODEL_REQUEST_FAILED",
        "message": "模型服务返回错误",
    }
    persisted = (workspace.root / ".agentkit/runs" / f"{run.id}.json").read_text()
    assert "Authorization" not in persisted


def test_context_manager_compacts_oldest_history_and_rejects_oversize():
    manager = ContextManager()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "a" * 2200},
        {"role": "assistant", "content": "b" * 2200},
        {"role": "user", "content": "current"},
    ]

    compacted, changed = manager.fit(
        messages,
        max_input_tokens=1500,
        reserve_output_tokens=200,
        threshold_ratio=0.8,
    )

    assert changed
    assert compacted[0]["role"] == "system"
    assert compacted[-2]["role"] == "assistant"
    assert compacted[-1]["content"] == "current"


@pytest.mark.asyncio
async def test_runtime_emits_compaction_lifecycle_and_preserves_recent_history(
    tmp_path: Path,
):
    workspace, build = _workspace_and_build(
        tmp_path,
        context=ContextSpec(
            max_input_tokens=1024,
            reserve_output_tokens=128,
            compaction=CompactionSpec(enabled=True, threshold_ratio=0.5),
        ),
    )
    previous_output = "b" * 1000
    client = FakeModelClient(
        [
            ModelResponse(
                content=previous_output,
                finish_reason="stop",
                usage=Usage(total_tokens=1),
                tool_calls=[],
                raw_message={"role": "assistant", "content": previous_output},
            ),
            ModelResponse(
                content="done",
                finish_reason="stop",
                usage=Usage(total_tokens=1),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "done"},
            ),
        ]
    )
    runtime = LocalAgentRuntime(workspace, model_client=client)
    session_id = "ses_compaction"

    await runtime.run(build.id, "a" * 1000, session_id=session_id)
    second = await runtime.run(build.id, "current", session_id=session_id)

    event_types = {event.type for event in runtime.event_store.events(second.id)}
    assert {
        "context.compaction.started",
        "context.compaction.completed",
    } <= event_types
    second_messages = client.calls[1][1]["messages"]
    assert second_messages[-2]["content"] == previous_output
    assert second_messages[-1]["content"] == "current"


def test_context_manager_honors_disabled_compaction():
    manager = ContextManager()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "a" * 1800},
        {"role": "user", "content": "current"},
    ]

    unchanged, changed = manager.fit(
        messages,
        max_input_tokens=1000,
        reserve_output_tokens=200,
        threshold_ratio=0.5,
        enabled=False,
    )

    assert unchanged == messages
    assert not changed

    with pytest.raises(StudioError, match="上下文压缩已关闭"):
        manager.fit(
            messages,
            max_input_tokens=450,
            reserve_output_tokens=100,
            threshold_ratio=0.5,
            enabled=False,
        )


def test_event_store_supports_last_event_id_semantics(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    store = RunEventStore(workspace)
    from ksadk.studio.contracts import RunRecord

    record = RunRecord(
        id="run_demo",
        build_id=build.id,
        agent_id="demo-agent",
        session_id="ses_demo",
        trace_id="trace_demo",
        input="hello",
    )
    store.create(record)
    store.append(record.id, "run.created", {})
    store.append(record.id, "run.started", {})

    assert [event.id for event in store.events(record.id, after=1)] == [2]


@pytest.mark.asyncio
async def test_runtime_injects_packaged_skill_instructions(tmp_path: Path):
    skill = tmp_path / "capabilities/skills/research"
    skill.mkdir(parents=True)
    (skill / "skill.yaml").write_text(
        "name: research\ninstructionsFile: SKILL.md\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "Always cite primary sources.",
        encoding="utf-8",
    )
    workspace, build = _workspace_and_build(
        tmp_path,
        skills=[CapabilityRef(name="research", version="1.0.0")],
    )
    client = FakeModelClient(
        [
            ModelResponse(
                content="done",
                finish_reason="stop",
                usage=Usage(total_tokens=1),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "done"},
            )
        ]
    )

    run = await LocalAgentRuntime(workspace, model_client=client).run(
        build.id,
        "research",
    )

    assert run.status == "COMPLETED"
    system = client.calls[0][1]["messages"][0]["content"]
    assert "Installed skills:" in system
    assert "Always cite primary sources." in system


@pytest.mark.asyncio
async def test_runtime_dispatches_mcp_tool_through_adapter(tmp_path: Path):
    server = MCPServerRef(
        name="demo-mcp",
        version="1.0.0",
        transport="stdio",
        command="demo-mcp",
    )
    tool = ToolContract(
        name="mcp_echo",
        version="1.0.0",
        executor="mcp",
        mcp_server="demo-mcp",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    workspace, build = _workspace_and_build(
        tmp_path,
        tools=[tool],
        mcp_servers=[server],
    )
    tool_call = {
        "id": "call_mcp",
        "type": "function",
        "function": {"name": "mcp_echo", "arguments": '{"value":"hello"}'},
    }
    client = FakeModelClient(
        [
            ModelResponse(
                content="",
                finish_reason="tool_calls",
                usage=Usage(total_tokens=1),
                tool_calls=[
                    ToolCall(
                        id="call_mcp",
                        name="mcp_echo",
                        arguments='{"value":"hello"}',
                        raw=tool_call,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
            ),
            ModelResponse(
                content="hello",
                finish_reason="stop",
                usage=Usage(total_tokens=1),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "hello"},
            ),
        ]
    )

    class FakeMCPRuntime:
        def __init__(self):
            self.calls = []

        async def call(self, server, **kwargs):
            self.calls.append((server, kwargs))
            return {"content": [{"type": "text", "text": "hello"}]}

    mcp_runtime = FakeMCPRuntime()
    runtime = LocalAgentRuntime(
        workspace,
        model_client=client,
        mcp_runtime=mcp_runtime,
    )

    run = await runtime.run(build.id, "call mcp")

    assert run.status == "COMPLETED"
    assert mcp_runtime.calls[0][0].name == "demo-mcp"
    assert mcp_runtime.calls[0][1]["arguments"] == {"value": "hello"}


@pytest.mark.asyncio
async def test_plan_act_observe_emits_explicit_plan_event(tmp_path: Path):
    workspace, build = _workspace_and_build(
        tmp_path,
        strategy="plan-act-observe",
    )
    client = FakeModelClient(
        [
            ModelResponse(
                content="done",
                finish_reason="stop",
                usage=Usage(total_tokens=1),
                tool_calls=[],
                raw_message={"role": "assistant", "content": "done"},
            )
        ]
    )
    runtime = LocalAgentRuntime(workspace, model_client=client)

    run = await runtime.run(build.id, "plan this")

    plan = next(
        event
        for event in runtime.event_store.events(run.id)
        if event.type == "plan.created"
    )
    assert plan.data == {"strategy": "plan-act-observe", "maxSteps": 12}


@pytest.mark.asyncio
async def test_runtime_enforces_tool_approval_before_execution(tmp_path: Path):
    tool = ToolContract(
        name="dangerous_write",
        version="1.0.0",
        permissions=["records:write"],
        side_effect="write",
        approval="always",
    )
    workspace, build = _workspace_and_build(
        tmp_path,
        tools=[tool],
        allowed_permissions=["records:write"],
    )
    raw_call = {
        "id": "call_write",
        "type": "function",
        "function": {"name": "dangerous_write", "arguments": "{}"},
    }
    client = FakeModelClient(
        [
            ModelResponse(
                content="",
                finish_reason="tool_calls",
                usage=Usage(total_tokens=1),
                tool_calls=[
                    ToolCall(
                        id="call_write",
                        name="dangerous_write",
                        arguments="{}",
                        raw=raw_call,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [raw_call],
                },
            )
        ]
    )

    run = await LocalAgentRuntime(workspace, model_client=client).run(
        build.id,
        "write",
    )

    assert run.status == "FAILED"
    assert run.error["code"] == "TOOL_APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_workspace_builtin_tools_read_write_edit_glob_and_grep(
    tmp_path: Path,
):
    contracts = builtin_tool_contracts()
    names = [
        "workspace.read",
        "workspace.write",
        "workspace.edit",
        "workspace.glob",
        "workspace.grep",
    ]
    tools = [
        contracts[name].model_copy(update={"approval": "never"})
        for name in names
    ]
    permissions = sorted(
        {permission for tool in tools for permission in tool.permissions}
    )
    workspace, build = _workspace_and_build(
        tmp_path,
        tools=tools,
        allowed_permissions=permissions,
    )
    runtime = LocalAgentRuntime(workspace, model_client=FakeModelClient([]))
    assert build.artifact_path
    resolved = runtime._load_resolved(build.artifact_path)

    written = await runtime._execute_tool(
        ToolCall(
            id="write",
            name="workspace.write",
            arguments='{"path":"notes/demo.txt","content":"alpha beta"}',
        ),
        resolved,
    )
    read = await runtime._execute_tool(
        ToolCall(
            id="read",
            name="workspace.read",
            arguments='{"path":"notes/demo.txt"}',
        ),
        resolved,
    )
    edited = await runtime._execute_tool(
        ToolCall(
            id="edit",
            name="workspace.edit",
            arguments=(
                '{"path":"notes/demo.txt","oldText":"beta","newText":"gamma"}'
            ),
        ),
        resolved,
    )
    globbed = await runtime._execute_tool(
        ToolCall(
            id="glob",
            name="workspace.glob",
            arguments='{"pattern":"notes/*.txt"}',
        ),
        resolved,
    )
    grepped = await runtime._execute_tool(
        ToolCall(
            id="grep",
            name="workspace.grep",
            arguments=(
                '{"query":"gamma","filePattern":"notes/*.txt"}'
            ),
        ),
        resolved,
    )

    assert written["bytesWritten"] == len("alpha beta")
    assert read["content"] == "alpha beta"
    assert edited["replacements"] == 1
    assert globbed["matches"] == ["notes/demo.txt"]
    assert grepped["matches"][0] == {
        "path": "notes/demo.txt",
        "line": 1,
        "text": "alpha gamma",
    }

    with pytest.raises(StudioError) as captured:
        await runtime._execute_tool(
            ToolCall(
                id="escape",
                name="workspace.read",
                arguments='{"path":"../secret.txt"}',
            ),
            resolved,
        )
    assert captured.value.code == "WORKSPACE_PATH_FORBIDDEN"


@pytest.mark.asyncio
async def test_runtime_rejects_tool_output_that_violates_contract(tmp_path: Path):
    tool = ToolContract(
        name="builtin.echo",
        version="1.0.0",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        output_schema={
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        },
    )
    workspace, build = _workspace_and_build(tmp_path, tools=[tool])
    raw_call = {
        "id": "call_invalid_output",
        "type": "function",
        "function": {
            "name": "builtin.echo",
            "arguments": '{"value":"hello"}',
        },
    }
    client = FakeModelClient(
        [
            ModelResponse(
                content="",
                finish_reason="tool_calls",
                usage=Usage(total_tokens=1),
                tool_calls=[
                    ToolCall(
                        id="call_invalid_output",
                        name="builtin.echo",
                        arguments='{"value":"hello"}',
                        raw=raw_call,
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [raw_call],
                },
            )
        ]
    )

    run = await LocalAgentRuntime(workspace, model_client=client).run(
        build.id,
        "echo",
    )

    assert run.status == "FAILED"
    assert run.error["code"] == "TOOL_OUTPUT_INVALID"
