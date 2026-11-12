import json
import zipfile
from pathlib import Path

import pytest

from ksadk.a2a.composition import compose_project, load_source_module, prepare_composed_state
from ksadk.studio.a2a_orchestration import compile_orchestration
from tests.studio.test_a2a_orchestration import draft


LOCAL_SOURCE = '''from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage

def ksadk_graph_factory(*, checkpointer, resource_tools=()):
    graph = StateGraph(MessagesState)
    graph.add_node("answer", lambda state: {"messages": [AIMessage(content="local result")]})
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    return graph.compile(checkpointer=checkpointer)

root_agent = ksadk_graph_factory(checkpointer=MemorySaver())
'''


def build_bundle(tmp_path, source=LOCAL_SOURCE):
    from ksadk.studio.builder import AgentBundleBuilder
    from ksadk.studio.contracts import Instructions, ModelSpec, NetworkPolicy, SecuritySpec
    from ksadk.studio.workspace import Workspace
    workspace = Workspace(tmp_path)
    workspace.initialize()
    project = tmp_path / "project"
    project.mkdir()
    (project / "agent.py").write_text(source)
    definition = draft()
    from tests.a2a.test_invocation_service_r85 import request
    from ksadk.studio.contracts import RemoteAgentBinding
    fixture_spec = request().spec
    remote = RemoteAgentBinding.freeze(
        space_id=fixture_spec.space_id, target_agent_id=fixture_spec.target_agent_id,
        expected_version_id=fixture_spec.expected_version_id, local_name=fixture_spec.local_name,
        exposure=fixture_spec.exposure, policy_ref="policy://a2a/runtime",
    )
    definition.spec.bindings.remote_agents = [remote]
    definition.spec.a2a_orchestration.nodes[1].binding_id = remote.binding_id
    definition.spec.runtime.project_path = "project"
    definition.spec.instructions = Instructions(system="Review the user's request.")
    definition.spec.model = ModelSpec(model="fixture", endpoint_url="https://model.example.com/v1/chat/completions", credential_ref="env://MODEL_KEY")
    definition.spec.security = SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"]))
    record = AgentBundleBuilder(workspace).build(definition)
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(workspace.resolve(record.artifact_path)) as archive:
        archive.extractall(extracted)
    return definition, record, extracted


@pytest.mark.asyncio
async def test_bundle_launch_uses_composition_common_service_and_replays_once(tmp_path):
    from ksadk.a2a.invocation_types import A2AInvocationRequest, RemoteAgentSpec
    from ksadk.a2a.langgraph_managed import bind_managed_langgraph
    from tests.a2a.test_invocation_service_r85 import request, service, trigger
    definition, record, extracted = build_bundle(tmp_path)
    from ksadk.studio.hosted_kernel import preflight_hosted_kernel_bundle
    preflight_hosted_kernel_bundle((tmp_path / record.artifact_path).read_bytes())
    config = json.loads((extracted / "runtime/agentengine.yaml").read_text())
    assert config["entry_point"] == record.runtime_lock["entryPoint"] == "studio_a2a_entry.py"
    assert config["agent_variable"] == record.runtime_lock["agentVariable"] == "root_agent"
    assert record.runtime_lock["originalEntryPoint"] == "agent.py"
    module = load_source_module(extracted / "runtime", config["entry_point"])
    assert set(module.root_agent.nodes) >= {"local", "remote"}
    native_saver = module.root_agent.checkpointer
    assert native_saver is not None
    graph = module.ksadk_graph_factory(checkpointer=native_saver)
    compiled = compile_orchestration(definition)
    spec = RemoteAgentSpec.model_validate(compiled.plan["exposure"]["entries"][0]["spec"])
    payload = request().model_dump()
    payload["spec"] = spec.model_dump()
    payload["handle"].update(binding_id=spec.binding_id, target_agent_id=spec.target_agent_id)
    req = A2AInvocationRequest.model_validate(payload)
    svc, store, wire = await service()
    facts = []
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), facts.append):
        state = module.ksadk_prepare_state({"input": "review"}, {"history": ["must not repeat"]})
        result = await graph.ainvoke(state, {"configurable": {"thread_id": "composed-run"}})
        # Re-entering the same durable invocation replays its original result.
        replay = await graph.ainvoke(state, {"configurable": {"thread_id": "composed-replay"}})
    assert [message.content for message in result["messages"]] == ["review", "local result", "hello"]
    assert replay["messages"][-1].content == "hello"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
    assert facts and await store.list("s", run_id=req.handle.run_id)


def test_source_packages_are_isolated_and_relative_imports_work(tmp_path):
    modules = []
    for name in ("first", "second"):
        root = tmp_path / name
        (root / "pkg").mkdir(parents=True)
        (root / "pkg/__init__.py").write_text("")
        (root / "pkg/helper.py").write_text(f"value = {name!r}\n")
        (root / "pkg/agent.py").write_text("from .helper import value\nroot_agent = value\n")
        modules.append(load_source_module(root, "pkg/agent.py"))
    assert [module.root_agent for module in modules] == ["first", "second"]
    with pytest.raises(ValueError, match="A2A_SOURCE_ENTRY_INVALID"):
        load_source_module(tmp_path, "../outside.py")


def test_checkpoint_and_custom_state_require_explicit_safe_integration():
    from types import SimpleNamespace
    source = SimpleNamespace(root_agent=SimpleNamespace(checkpointer=object()))
    with pytest.raises(ValueError, match="A2A_LOCAL_CHECKPOINTER_MUST_INHERIT"):
        compose_project(source, lambda *_: None, framework="langgraph", local_ids=["local"], handoff_sources=[], agent_variable="root_agent")
    with pytest.raises(ValueError, match="A2A_PREPARE_STATE_HOOK_REQUIRED"):
        prepare_composed_state(source, {"input": [{"image": "fixture"}]}, {})
    seen = []
    source.ksadk_prepare_state = lambda payload, context: seen.append((payload, context)) or {"custom": 1}
    assert prepare_composed_state(source, {"input": "new"}, {"session": "s"}) == {"custom": 1}
    assert seen == [({"input": "new"}, {"session": "s"})]
