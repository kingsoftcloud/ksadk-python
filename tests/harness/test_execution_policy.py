from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.execution_policy import ExecutionPolicy, current_execution_policy
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import ExecutionStrategySpec, HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.tools import HarnessTool
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


def spec():
    return HarnessSpec(
        agent_revision_ref="agent-revision://policy@1",
        model=ModelBinding(profile_ref="model-profile://fake@1"),
        prompt=PromptSpec(instructions="base"),
        execution_strategy=ExecutionStrategySpec(config={"max_tool_calls": 2}),
    )


def request(ref):
    return StartRequest(
        input="go",
        user_id="user",
        agent_id="agent",
        session_id=ref,
        metadata={"execution_policy_ref": ref, "invocation_id": ref},
    )


def tool(name, handler):
    return HarnessTool(
        name=name, description=name, parameters={"type": "object"}, handler=handler, source="host"
    )


class PolicyReasoner:
    async def complete(self, *, prompt, messages, tools, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            return HarnessReasoningTurn(final_text="done")
        name = prompt.splitlines()[-1]
        assert [t.name for t in tools] == [name]
        return HarnessReasoningTurn(tool_calls=(HarnessToolCall("call", name, {}),))


class Resolver:
    def __init__(self, policies):
        self.policies = policies
        self.seen = []
        self.revoked = False

    async def resolve(self, ref, *, request):
        self.seen.append((ref, request.user_id, request.session_id))
        if self.revoked or request.user_id != "user" or request.session_id != ref:
            raise PermissionError("execution policy revoked or outside scope")
        return self.policies[ref]


@pytest.mark.asyncio
async def test_concurrent_run_policies_isolate_tools_prompt_and_workspace(tmp_path):
    seen = []

    async def invoke(arguments, call_id):
        policy = current_execution_policy()
        await asyncio.sleep(0)
        assert current_execution_policy() is policy
        seen.append(policy.workspace_root.name)
        return policy.workspace_root.name

    resolver = Resolver(
        {
            name: ExecutionPolicy(
                system_context=name,
                tools={name: tool(name, invoke)},
                workspace_root=tmp_path / name,
                limits={"max_tool_calls": 20},
            )
            for name in ("one", "two")
        }
    )
    engine = ManagedLangGraphEngine(reasoner=PolicyReasoner(), checkpointer=memory_checkpointer())
    adapter = ManagedHarnessRuntimeAdapter(
        spec(), engine=engine, execution_policy_resolver=resolver
    )
    handles = [await adapter.start(request(name)) for name in ("one", "two")]

    async def consume(handle):
        return [e async for e in adapter.stream(handle)]

    await asyncio.gather(*(consume(h) for h in handles))
    assert sorted(seen) == ["one", "two"]
    assert engine._tools == {}
    assert adapter.harness_spec.prompt.instructions == "base"
    assert all(
        run.compiled.spec.execution_strategy.config["max_tool_calls"] == 2
        for run in engine._runs.values()
    )
    assert adapter.capabilities().execution_policy.supported
    assert current_execution_policy() is None


@pytest.mark.asyncio
async def test_policy_ref_without_resolver_fails_closed():
    adapter = ManagedHarnessRuntimeAdapter(spec(), engine=ManagedLangGraphEngine())
    assert not adapter.capabilities().execution_policy.supported
    with pytest.raises(UnsupportedControlError, match="host resolver"):
        await adapter.start(request("one"))
    assert adapter._engine._runs == {}


@pytest.mark.asyncio
async def test_resolver_rechecked_before_tool_side_effect():
    async def never(arguments, call_id):
        pytest.fail("revoked policy must not execute")

    resolver = Resolver(
        {"one": ExecutionPolicy(system_context="one", tools={"one": tool("one", never)})}
    )

    class RevokeAfterModel(PolicyReasoner):
        async def complete(self, **kwargs):
            result = await super().complete(**kwargs)
            resolver.revoked = True
            return result

    engine = ManagedLangGraphEngine(reasoner=RevokeAfterModel(), checkpointer=memory_checkpointer())
    adapter = ManagedHarnessRuntimeAdapter(
        spec(), engine=engine, execution_policy_resolver=resolver
    )
    handle = await adapter.start(request("one"))
    events = [e async for e in adapter.stream(handle)]
    assert any(e.event_type == "run.failed" for e in events)
    assert len(resolver.seen) >= 3


@pytest.mark.asyncio
async def test_policy_ref_restored_and_revalidated_for_approval_resume():
    calls = []

    async def invoke(arguments, call_id):
        calls.append(1)
        return "ok"

    resolver = Resolver(
        {
            "one": ExecutionPolicy(
                system_context="one",
                tools={"one": tool("one", invoke)},
                approval_required=frozenset({"one"}),
            )
        }
    )
    saver = memory_checkpointer()

    def make():
        return ManagedHarnessRuntimeAdapter(
            spec(),
            engine=ManagedLangGraphEngine(reasoner=PolicyReasoner(), checkpointer=saver),
            durable=True,
            execution_policy_resolver=resolver,
        )

    first = make()
    handle = await first.start(request("one"))
    assert handle.native_ref["execution_policy_ref"] == "one"
    assert [e async for e in first.stream(handle)]
    assert calls == []
    second = make()
    await second.attach(handle)
    await second.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
        ResumePayload(kind="approval_decision", call_id="call", data="approved"),
    )
    assert [e async for e in second.stream(handle)]
    assert calls == [1]
    resolver.revoked = True
    with pytest.raises(PermissionError):
        await make().attach(handle)


@pytest.mark.asyncio
async def test_native_child_write_obeys_parent_approval_and_policy_workspace(tmp_path):
    from ksadk.harness.spec import SubAgentBinding
    from ksadk.plugins.providers.harness_tools import assemble_python_tools

    tools, approvals = assemble_python_tools(
        tmp_path,
        {
            "capabilities": {
                "tools": [
                    {
                        "name": "write_workspace_file",
                        "executor": "builtin",
                        "sideEffect": "write",
                    }
                ]
            }
        },
        workspace_root=tmp_path / "base",
    )

    class NativeReasoner:
        async def complete(self, *, prompt, messages, **kwargs):
            if any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(final_text="done")
            call = (
                HarnessToolCall(
                    "native-write",
                    "write_workspace_file",
                    {
                        "path": "proof.txt",
                        "content": "approved child output",
                    },
                )
                if prompt == "child"
                else HarnessToolCall("delegate", "child", {"task": "write"})
            )
            return HarnessReasoningTurn(tool_calls=(call,))

    policy = ExecutionPolicy(workspace_root=tmp_path / "policy")
    resolver = Resolver({"one": policy})
    engine = ManagedLangGraphEngine(
        reasoner=NativeReasoner(),
        checkpointer=memory_checkpointer(),
        tools=tools,
        approval_required=approvals,
    )
    parent_spec = spec().model_copy(
        update={
            "sub_agents": (
                SubAgentBinding(
                    name="child",
                    instructions="child",
                    tools=("write_workspace_file",),
                ),
            )
        }
    )
    adapter = ManagedHarnessRuntimeAdapter(
        parent_spec, engine=engine, execution_policy_resolver=resolver
    )
    handle = await adapter.start(request("one"))
    events = [event async for event in adapter.stream(handle)]
    assert any(e.event_type == "interaction.requested" for e in events)
    assert not list(tmp_path.rglob("proof.txt"))
    await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
        ResumePayload(kind="approval_decision", call_id="delegate", data="approved"),
    )
    events = [event async for event in adapter.stream(handle)]
    output = tmp_path / "policy/proof.txt"
    assert output.read_text() == "approved child output", events
    assert not (tmp_path / "base").exists()


@pytest.mark.asyncio
async def test_builtin_relative_paths_match_each_policy_workspace(tmp_path):
    from ksadk.harness.execution_policy import execution_policy_scope
    from ksadk.plugins.providers.harness_tools import assemble_python_tools

    tools, _ = assemble_python_tools(
        tmp_path,
        {"capabilities": {"tools": [{"name": "write_workspace_file", "executor": "builtin"}]}},
        workspace_root=tmp_path / "base",
    )

    async def write(member):
        root = tmp_path / member
        with execution_policy_scope(ExecutionPolicy(workspace_root=root)):
            await tools["write_workspace_file"].handler(
                {"path": "report.md", "content": member}, member
            )
        # Host tools such as artifact publication resolve the same relative path.
        assert (root / "report.md").read_text() == member
        assert list(root.rglob("report.md")) == [root / "report.md"]

    await asyncio.gather(write("one"), write("two"))
    assert not (tmp_path / "base").exists()


@pytest.mark.asyncio
async def test_managed_provider_compiles_declared_subagents_and_policy_capability(tmp_path):
    from ksadk.harness.config import HarnessConfig
    from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter

    resolver = Resolver({})
    adapter = await build_managed_provider_adapter(
        HarnessConfig(model="fake", prompt="parent"),
        agent_name="test",
        workspace_root=tmp_path,
        reasoner=PolicyReasoner(),
        tool_contracts={"sub_agents": [{"name": "reviewer", "instructions": "review"}]},
        execution_policy_resolver=resolver,
    )
    await adapter.preflight()
    assert adapter.harness_spec.sub_agents[0].name == "reviewer"
    assert adapter.capabilities().execution_policy.supported


def test_managed_durable_interaction_command_uses_checkpoint_provider():
    from ksadk.harness.managed_runtime import managed_harness_capabilities
    from ksadk.kernel.mapping import ensure_supported

    durable = managed_harness_capabilities(durable=True)
    assert durable.interaction_mode == "durable_resume"
    ensure_supported("submit_interaction", durable)
    assert not managed_harness_capabilities(durable=False).submit_interaction.supported


@pytest.mark.asyncio
@pytest.mark.parametrize("child_context", [None, "Only inspect the delegated file.", ""])
async def test_explicit_child_context_changes_prompt_without_weakening_governance(
    tmp_path, child_context
):
    from ksadk.harness.capability_runtime import CapabilityRuntime
    from ksadk.harness.spec import SubAgentBinding

    parent_context = "WHOLE_GROUP_SNAPSHOT: all members, messages and tasks"
    observed = []
    writes = []

    async def write(arguments, call_id):
        writes.append((call_id, current_execution_policy().workspace_root))
        return "written"

    async def authority_only(arguments, call_id):
        pytest.fail("child must not gain an undeclared host tool")

    policy = ExecutionPolicy(
        system_context=parent_context,
        child_system_context=child_context,
        tools={"authority_only": tool("authority_only", authority_only)},
        workspace_root=tmp_path / "isolated",
        limits={"max_tool_calls": 3, "max_total_tokens": 10_000},
        approval_required=frozenset({"write"}),
    )
    resolver = Resolver({"one": policy})
    capability_runtime = CapabilityRuntime()

    class Reasoner:
        async def complete(self, *, prompt, messages, tools, **kwargs):
            child = prompt.startswith("child instructions")
            observed.append((child, prompt, messages))
            if any(m["role"] == "tool" for m in messages):
                return HarnessReasoningTurn(
                    final_text="done", usage={"input_tokens": 2, "output_tokens": 1}
                )
            if child:
                assert {t.name for t in tools} == {"write"}
                assert [m["content"] for m in messages if m["role"] == "user"] == [
                    "inspect one file"
                ]
                call = HarnessToolCall("write-call", "write", {})
            else:
                assert parent_context in prompt
                call = HarnessToolCall("delegate", "child", {"task": "inspect one file"})
            return HarnessReasoningTurn(
                tool_calls=(call,), usage={"input_tokens": 2, "output_tokens": 1}
            )

    engine = ManagedLangGraphEngine(
        reasoner=Reasoner(),
        checkpointer=memory_checkpointer(),
        tools={"write": tool("write", write)},
        capability_runtime=capability_runtime,
    )
    revision = spec().model_copy(
        update={
            "execution_strategy": ExecutionStrategySpec(config={"max_tool_calls": 3}),
            "sub_agents": (
                SubAgentBinding(
                    name="child",
                    instructions="child instructions",
                    tools=("write",),
                    max_total_tokens=8_000,
                ),
            ),
        }
    )
    adapter = ManagedHarnessRuntimeAdapter(
        revision, engine=engine, execution_policy_resolver=resolver
    )
    handle = await adapter.start(request("one"))
    events = [event async for event in adapter.stream(handle)]
    assert any(event.event_type == "interaction.requested" for event in events)
    assert not writes
    root = engine._runs[adapter._internal_handle(handle).run_id]
    child_engine, child_handle = engine._active_subagent_runs[root.handle.run_id]["delegate"]
    child_run = child_engine._runs[child_handle.run_id]
    assert child_engine._capability_runtime is capability_runtime
    assert child_run.execution_policy_resolver is resolver
    assert child_run.execution_policy_request is root.execution_policy_request
    assert child_run.execution_policy.workspace_root == (tmp_path / "isolated").resolve()
    assert "write" in child_run.approval_required
    assert child_run.compiled.spec.execution_strategy.config["max_total_tokens"] == 8_000
    assert child_run.compiled.spec.execution_strategy.config["max_tool_calls"] == 3
    expected = parent_context if child_context is None else child_context
    assert child_run.compiled.spec.prompt.instructions == "child instructions" + (
        "\n\n" + expected if expected else ""
    )
    if child_context is not None:
        assert all(
            parent_context not in prompt + str(messages)
            for child, prompt, messages in observed
            if child
        )
    await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
        ResumePayload(kind="approval_decision", call_id="delegate", data="approved"),
    )
    assert [event async for event in adapter.stream(handle)]
    assert writes == [("write-call", (tmp_path / "isolated").resolve())]
    assert root.budget_usage["tools"] == 2 and child_run.budget_usage["tools"] == 1
    assert root.state.status.value == "completed"
    assert root.budget_usage["tokens"] == 12
