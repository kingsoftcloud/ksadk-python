"""Deterministic, route-free A2A sequential/handoff compilation."""
from __future__ import annotations

import json
from dataclasses import dataclass

from ksadk.a2a.exposure import compile_exposure_plan
from ksadk.a2a.invocation_types import RemoteAgentSpec
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.contracts import AgentDraft, A2AOrchestration, RemoteAgentBinding
from ksadk.studio.errors import StudioError
from ksadk.studio.a2a_bindings import require_execution_policy


@dataclass(frozen=True)
class CompiledA2AOrchestration:
    plan: dict
    source: str
    digest: str
    entry_source: str


def compile_orchestration(draft: AgentDraft) -> CompiledA2AOrchestration | None:
    declaration = draft.spec.a2a_orchestration
    if declaration is None:
        return None
    declaration = A2AOrchestration.model_validate(declaration.model_dump())
    framework = draft.spec.runtime.type if draft.spec.runtime else ""
    if framework not in {"adk", "langgraph"}:
        raise StudioError("A2A_COMPOSITION_UNSUPPORTED", "此编排需要 ADK 或 LangGraph Runtime", status_code=422)
    bindings = {row.binding_id: RemoteAgentBinding.model_validate(row.model_dump())
                for row in draft.spec.bindings.remote_agents}
    nodes = {node.node_id: node for node in declaration.nodes}
    remote_ids = [node.binding_id for node in nodes.values() if node.kind == "remote"]
    if len(set(remote_ids)) != len(remote_ids) or set(remote_ids) != set(bindings):
        raise StudioError("A2A_BINDING_NODE_MISMATCH", "每个远端绑定必须对应唯一设计节点", status_code=422)
    handoffs = sorted((edge.source, edge.target) for edge in declaration.edges if edge.kind == "handoff")
    handoff_targets = {target for _, target in handoffs}
    for source, target in handoffs:
        if nodes[source].kind != "local" or nodes[target].kind != "remote":
            raise StudioError("A2A_HANDOFF_INVALID", "handoff 必须从本地节点指向远端绑定", status_code=422)
    specs = {}
    for node in sorted(nodes.values(), key=lambda item: item.node_id):
        if node.kind != "remote":
            continue
        binding = bindings[node.binding_id]
        require_execution_policy(binding.policy_ref)
        specs[node.node_id] = RemoteAgentSpec(
            local_name=binding.local_name, binding_id=binding.binding_id,
            space_id=binding.space_id, target_agent_id=binding.target_agent_id,
            expected_version_id=binding.expected_version_id, exposure=binding.exposure,
            policy_ref=binding.policy_ref,
        )
    exposure = compile_exposure_plan(list(specs.values()), framework=framework, consumer_id=draft.metadata.id)
    for entry in exposure.entries:
        node_id = next(key for key, spec in specs.items() if spec.binding_id == entry.spec.binding_id)
        if node_id not in handoff_targets and entry.exposure != "native":
            raise StudioError("A2A_EXPOSURE_CONFLICT", "顺序远端节点需要原生调用方式", status_code=422)
    main_nodes = set(nodes) - handoff_targets
    sequential = {edge.source: edge.target for edge in declaration.edges if edge.kind == "sequential"}
    order = []
    current = declaration.entry_node_id
    while current is not None:
        order.append(current)
        current = sequential.get(current)
    if set(order) != main_nodes:
        raise StudioError("A2A_COMPOSITION_DISCONNECTED", "所有顺序节点必须从入口连成单链，handoff 目标不能进入顺序链", status_code=422)
    plan = {
        "schemaVersion": "a2a.orchestration/v1", "framework": framework,
        "entryNodeId": declaration.entry_node_id,
        "nodes": [node.model_dump(by_alias=True, exclude_none=True) for node in sorted(nodes.values(), key=lambda node: node.node_id)],
        "sequence": order, "handoffs": [{"source": source, "target": target} for source, target in handoffs],
        "bindings": [bindings[key].model_dump(by_alias=True) for key in sorted(bindings)],
        "exposure": exposure.model_dump(mode="json"),
    }
    source = _source(framework, order, handoffs, specs)
    entry_source = _entry_source(draft, plan)
    digest = sha256_digest(canonical_json({"plan": plan, "source": source, "entrySource": entry_source}))
    return CompiledA2AOrchestration(plan, source, digest, entry_source)


def _source(framework, order, handoffs, specs):
    # JSON and repr keep authored names/data out of executable syntax.
    declarations = {node_id: spec.model_dump(mode="json", exclude_none=True) for node_id, spec in specs.items()}
    lines = [
        '"""Generated A2A composition. Local factories explicitly accept managed tools."""',
        "import json", "from ksadk.a2a.invocation_types import RemoteAgentSpec",
        f"_SPECS = json.loads({json.dumps(declarations, sort_keys=True)!r})",
        f"_ORDER = {order!r}", f"_HANDOFFS = {handoffs!r}",
    ]
    if framework == "langgraph":
        lines += [
            "from langgraph.graph import StateGraph, MessagesState, START, END",
            "from ksadk.a2a.langgraph import remote_node, handoff_tool",
            "def compose(local_nodes, *, local_factories=None, checkpointer=None):",
            "    local_factories = local_factories or {}",
            "    nodes = dict(local_nodes)",
            "    tools = {}",
            "    for source, target in _HANDOFFS:",
            "        tools.setdefault(source, []).append(handoff_tool(RemoteAgentSpec(**_SPECS[target])))",
            "    for source, bindings in tools.items():",
            "        if source not in local_factories:",
            "            raise ValueError('A2A_LOCAL_TOOL_FACTORY_REQUIRED')",
            "        nodes[source] = local_factories[source](tuple(bindings))",
            "        if getattr(nodes[source], 'checkpointer', None) is not None:",
            "            raise ValueError('A2A_LOCAL_CHECKPOINTER_MUST_INHERIT')",
            "    graph = StateGraph(MessagesState)",
            "    for node_id in _ORDER:",
            "        node = remote_node(RemoteAgentSpec(**_SPECS[node_id])) if node_id in _SPECS else nodes[node_id]",
            "        graph.add_node(node_id, node)",
            "    graph.add_edge(START, _ORDER[0])",
            "    for source, target in zip(_ORDER, _ORDER[1:]):",
            "        graph.add_edge(source, target)",
            "    graph.add_edge(_ORDER[-1], END)",
            "    compiled = graph.compile(checkpointer=checkpointer)",
            "    from ksadk.a2a.langgraph_tool_runtime import graph_exposure_plan",
            "    actual = {entry.spec.binding_id: entry.spec for entry in graph_exposure_plan(compiled).entries}",
            "    expected = {value['binding_id']: RemoteAgentSpec(**value) for value in _SPECS.values()}",
            "    if actual != expected:",
            "        raise ValueError('A2A_COMPOSITION_EXPOSURE_MISMATCH')",
            "    return compiled",
        ]
    else:
        lines += [
            "from google.adk.agents import SequentialAgent",
            "from google.adk.agents import LlmAgent",
            "from ksadk.a2a.adk import remote_agent, handoff_tool",
            "from ksadk.a2a.adk_tools import adk_exposure_plan",
            "def compose(local_nodes):",
            "    local = {key: node.clone() for key, node in local_nodes.items()}",
            "    for source, target in _HANDOFFS:",
            "        node = local[source]",
            "        if not isinstance(node, LlmAgent):",
            "            raise ValueError('A2A_LOCAL_HANDOFF_REQUIRES_LLM_AGENT')",
            "        tool = handoff_tool(RemoteAgentSpec(**_SPECS[target]))",
            "        names = [getattr(item, 'name', getattr(item, '__name__', '')) for item in node.tools]",
            "        if tool.name in names:",
            "            raise ValueError('A2A_LOCAL_NAME_CONFLICT')",
            "        node.tools = [*node.tools, tool]",
            "    nodes = [remote_agent(RemoteAgentSpec(**_SPECS[key])) if key in _SPECS else local[key] for key in _ORDER]",
            "    compiled = SequentialAgent(name='studio_a2a_sequence', sub_agents=nodes)",
            "    actual = {entry.spec.binding_id: entry.spec for entry in adk_exposure_plan(compiled).entries}",
            "    expected = {value['binding_id']: RemoteAgentSpec(**value) for value in _SPECS.values()}",
            "    if actual != expected:",
            "        raise ValueError('A2A_COMPOSITION_EXPOSURE_MISMATCH')",
            "    return compiled",
        ]
    return "\n".join(lines) + "\n"


def _entry_source(draft, plan):
    runtime = draft.spec.runtime
    entry = runtime.entry_point or "agent.py"
    if entry in {"studio_a2a_entry.py", "studio_a2a_composition.py"}:
        raise StudioError("A2A_GENERATED_SOURCE_CONFLICT", "原入口不能使用 A2A 生成文件名", status_code=422)
    local_ids = [node["nodeId"] for node in plan["nodes"] if node["kind"] == "local"]
    handoff_sources = sorted({edge["source"] for edge in plan["handoffs"]})
    lines = [
        '"""Generated immutable A2A launch entry."""',
        "from pathlib import Path",
        "from ksadk.a2a.composition import load_source_module, compose_project, prepare_composed_state",
        "_root = Path(__file__).resolve().parent",
        f"_source = load_source_module(_root, {entry!r})",
        "_compose = load_source_module(_root, 'studio_a2a_composition.py').compose",
        f"_options = {{'framework': {runtime.type!r}, 'local_ids': {local_ids!r}, 'handoff_sources': {handoff_sources!r}, 'agent_variable': {runtime.agent_variable!r}}}",
    ]
    if runtime.type == "langgraph":
        lines += [
            "from langgraph.checkpoint.memory import MemorySaver",
            "def ksadk_graph_factory(*, checkpointer, resource_tools=()):",
            "    return compose_project(_source, _compose, **_options, checkpointer=checkpointer, resource_tools=resource_tools)",
            "def ksadk_prepare_state(payload, session_context):",
            "    return prepare_composed_state(_source, payload, session_context)",
            "root_agent = ksadk_graph_factory(checkpointer=MemorySaver())",
        ]
    else:
        lines += ["root_agent = compose_project(_source, _compose, **_options)"]
    return "\n".join(lines) + "\n"
