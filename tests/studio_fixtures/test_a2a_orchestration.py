
import pytest
from pydantic import ValidationError

from ksadk.studio.a2a_orchestration import compile_orchestration
from ksadk.studio.contracts import (
    A2AOrchestration, AgentBindings, AgentDraft, AgentMetadata, AgentSpec,
    RemoteAgentBinding, RuntimeRef,
)
from ksadk.studio.errors import StudioError


def draft(*, handoff=False, framework="langgraph"):
    binding = RemoteAgentBinding.freeze(
        space_id="as-000000000001", target_agent_id="00000000000040008000000000000001",
        expected_version_id="00000000000040008000000000000002",
        local_name="reviewer", policy_ref="policy://a2a/runtime", exposure="auto",
    )
    return AgentDraft(metadata=AgentMetadata(id="composition", name="Composition"), spec=AgentSpec(
        runtime=RuntimeRef(type=framework, project_path=".", entry_point="agent.py"),
        bindings=AgentBindings(remote_agents=[binding]),
        a2a_orchestration=A2AOrchestration.model_validate({
            "entryNodeId": "local",
            "nodes": [{"nodeId": "local", "kind": "local"}, {"nodeId": "remote", "kind": "remote", "bindingId": binding.binding_id}],
            "edges": [{"source": "local", "target": "remote", "kind": "handoff" if handoff else "sequential"}],
        }),
    ))


def test_deterministic_route_free_source_and_binding_node_plan():
    first = draft()
    reordered = first.model_copy(deep=True)
    reordered.spec.a2a_orchestration.nodes = tuple(reversed(first.spec.a2a_orchestration.nodes))
    a, b = compile_orchestration(first), compile_orchestration(reordered)
    assert a == b
    assert a.plan["sequence"] == ["local", "remote"]
    assert a.plan["nodes"][1]["bindingId"] == first.spec.bindings.remote_agents[0].binding_id
    assert "from ksadk.a2a.langgraph import remote_node, handoff_tool" in a.source
    for forbidden in ("https://", "http://", "A2AClient", "credential", "permit"):
        assert forbidden not in a.source
    compile(a.source, "generated.py", "exec")


def test_generated_langgraph_is_a_real_graph_with_fixed_remote_node():
    compiled = compile_orchestration(draft())
    namespace = {}
    exec(compiled.source, namespace)
    graph = namespace["compose"]({"local": lambda state: {"messages": []}})
    assert {"local", "remote"} <= graph.nodes.keys()
    from ksadk.a2a.langgraph_tool_runtime import graph_exposure_plan
    assert graph_exposure_plan(graph).entries[0].spec.binding_id == draft().spec.bindings.remote_agents[0].binding_id
    import asyncio
    with pytest.raises(RuntimeError, match="A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"):
        asyncio.run(graph.ainvoke({"messages": [("human", "Review this")]}))


def test_handoff_requires_explicit_local_factory_and_preserves_pin():
    compiled = compile_orchestration(draft(handoff=True))
    namespace = {}
    exec(compiled.source, namespace)
    with pytest.raises(ValueError, match="A2A_LOCAL_TOOL_FACTORY_REQUIRED"):
        namespace["compose"]({})
    captured = []
    def factory(tools):
        captured.extend(tools)
        from langgraph.graph import StateGraph, MessagesState, START, END
        from langgraph.prebuilt import ToolNode
        graph = StateGraph(MessagesState)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        return graph.compile()
    graph = namespace["compose"]({}, local_factories={"local": factory})
    assert "remote" not in graph.nodes
    assert captured[0].metadata["ksadk_a2a_remote_spec"]["expected_version_id"] == draft().spec.bindings.remote_agents[0].expected_version_id


def test_generated_adk_sequence_contains_managed_remote_agent():
    compiled = compile_orchestration(draft(framework="adk"))
    namespace = {}
    exec(compiled.source, namespace)
    from google.adk.agents import LlmAgent
    from ksadk.a2a.adk import A2ARemoteAgent
    agent = namespace["compose"]({"local": LlmAgent(name="local", model="fixture")})
    assert isinstance(agent.sub_agents[1], A2ARemoteAgent)


@pytest.mark.parametrize("kind", ["parallel", "conditional", "loop", "external"])
def test_unsupported_edges_are_rejected(kind):
    payload = draft().spec.a2a_orchestration.model_dump()
    payload["edges"][0]["kind"] = kind
    with pytest.raises(ValidationError):
        A2AOrchestration.model_validate(payload)


def test_cycle_is_rejected_even_outside_entry_component():
    payload = draft().spec.a2a_orchestration.model_dump()
    payload["nodes"] = list(payload["nodes"]) + [{"node_id": "unused", "kind": "local"}]
    payload["edges"] = [{"source": "remote", "target": "unused", "kind": "sequential"}, {"source": "unused", "target": "remote", "kind": "sequential"}]
    with pytest.raises(ValidationError, match="A2A_LOOP_UNSUPPORTED"):
        A2AOrchestration.model_validate(payload)


def test_disconnected_or_missing_binding_cannot_compile():
    disconnected = draft()
    disconnected.spec.a2a_orchestration.edges = ()
    with pytest.raises(StudioError, match="所有顺序节点"):
        compile_orchestration(disconnected)
    missing = draft()
    missing.spec.bindings.remote_agents = []
    with pytest.raises(StudioError, match="唯一设计节点"):
        compile_orchestration(missing)


def test_model_copy_cannot_bypass_frozen_digest_validation():
    tampered = draft()
    original = tampered.spec.bindings.remote_agents[0]
    tampered.spec.bindings.remote_agents[0] = original.model_copy(update={"local_name": "changed"})
    with pytest.raises(ValidationError, match="A2A_BINDING_DIGEST_MISMATCH"):
        compile_orchestration(tampered)


@pytest.mark.parametrize("collision", ["studio_a2a_composition.py", "studio_a2a_entry.py"])
def test_bundle_contains_digested_generated_code_and_refuses_source_collision(tmp_path, collision):
    import json
    import zipfile
    from ksadk.studio.builder import AgentBundleBuilder
    from ksadk.studio.contracts import ModelSpec, Instructions, SecuritySpec, NetworkPolicy
    from ksadk.studio.workspace import Workspace
    workspace = Workspace(tmp_path)
    workspace.initialize()
    source = tmp_path / "project"
    source.mkdir()
    (source / "agent.py").write_text("root_agent = None\n")
    definition = draft()
    definition.spec.runtime.project_path = "project"
    definition.spec.instructions = Instructions(system="Review requests.")
    definition.spec.model = ModelSpec(model="fixture", endpoint_url="https://model.example.com/v1/chat/completions", credential_ref="env://MODEL_KEY")
    definition.spec.security = SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"]))
    result = AgentBundleBuilder(workspace).build(definition)
    with zipfile.ZipFile(workspace.resolve(result.artifact_path)) as archive:
        generated = archive.read("runtime/studio_a2a_composition.py").decode()
        lock = json.loads(archive.read("agentkit.lock"))
        resolved = json.loads(archive.read("resolved-agent-spec.json"))
    compiled = compile_orchestration(definition)
    assert generated == compiled.source
    assert lock["a2aOrchestration"]["digest"] == compiled.digest
    assert resolved["a2aOrchestration"] == lock["a2aOrchestration"]
    assert AgentBundleBuilder(workspace).build(definition).id == result.id
    (source / collision).write_text("# user's code\n")
    with pytest.raises(ValueError, match="A2A_GENERATED_SOURCE_CONFLICT"):
        AgentBundleBuilder(workspace).build(definition)
    assert (source / collision).read_text() == "# user's code\n"


def test_handoff_factory_cannot_silently_drop_declared_remote_tool():
    namespace = {}
    exec(compile_orchestration(draft(handoff=True)).source, namespace)
    with pytest.raises(ValueError, match="A2A_COMPOSITION_EXPOSURE_MISMATCH"):
        namespace["compose"]({}, local_factories={"local": lambda tools: lambda state: {"messages": []}})


def test_adk_handoff_composition_preserves_original_agent_and_rejects_tool_collision():
    from google.adk.agents import LlmAgent, SequentialAgent
    from ksadk.a2a.adk_tools import ManagedA2AHandoffTool
    namespace = {}
    exec(compile_orchestration(draft(handoff=True, framework="adk")).source, namespace)
    original = LlmAgent(name="supervisor", model="fixture")
    first = namespace["compose"]({"local": original})
    second = namespace["compose"]({"local": original})
    assert first is not second and first.sub_agents[0] is not original
    assert original.tools == [] and original.parent_agent is None
    assert isinstance(first.sub_agents[0].tools[0], ManagedA2AHandoffTool)
    async def reviewer(message: str):
        return message
    conflicting = LlmAgent(name="conflicting", model="fixture", tools=[reviewer])
    with pytest.raises(ValueError, match="A2A_LOCAL_NAME_CONFLICT"):
        namespace["compose"]({"local": conflicting})
    with pytest.raises(ValueError, match="A2A_LOCAL_HANDOFF_REQUIRES_LLM_AGENT"):
        namespace["compose"]({"local": SequentialAgent(name="not_llm")})


def test_unknown_execution_policy_ref_cannot_compile():
    definition = draft()
    current = definition.spec.bindings.remote_agents[0]
    definition.spec.bindings.remote_agents[0] = RemoteAgentBinding.freeze(
        binding_id=current.binding_id, space_id=current.space_id,
        target_agent_id=current.target_agent_id, expected_version_id=current.expected_version_id,
        local_name=current.local_name, exposure=current.exposure,
        policy_ref="policy://missing-policy",
    )
    with pytest.raises(StudioError, match="A2A_POLICY_REF_UNSUPPORTED"):
        compile_orchestration(definition)


def test_generated_remote_spec_preserves_executable_policy_ref():
    definition = draft()
    current = definition.spec.bindings.remote_agents[0]
    definition.spec.bindings.remote_agents[0] = RemoteAgentBinding.freeze(
        binding_id=current.binding_id, space_id=current.space_id,
        target_agent_id=current.target_agent_id, expected_version_id=current.expected_version_id,
        local_name=current.local_name, exposure=current.exposure,
        policy_ref="policy://a2a/strict",
    )
    compiled = compile_orchestration(definition)
    namespace = {}
    exec(compiled.source, namespace)
    assert namespace["_SPECS"]["remote"]["policy_ref"] == "policy://a2a/strict"
