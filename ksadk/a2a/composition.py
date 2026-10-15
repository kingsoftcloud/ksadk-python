"""Load immutable Studio compositions without modifying code-first projects."""
from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path
from types import ModuleType


def load_source_module(root: Path, entry_point: str):
    root = root.resolve()
    relative = entry_point[:-3] if entry_point.endswith(".py") else entry_point
    parts = relative.replace("\\", "/").replace("/", ".").split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        raise ValueError("A2A_SOURCE_ENTRY_INVALID")
    source = root.joinpath(*parts).with_suffix(".py").resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("A2A_SOURCE_ENTRY_INVALID")
    # Each bundle gets its own package namespace, including relative imports.
    namespace = "_ksadk_a2a_bundle_" + hashlib.sha256(str(root).encode()).hexdigest()[:24]
    if namespace not in sys.modules:
        package = ModuleType(namespace)
        package.__path__ = [str(root)]
        package.__package__ = namespace
        sys.modules[namespace] = package
    return importlib.import_module(namespace + "." + ".".join(parts))


def compose_project(source, compose, *, framework, local_ids, handoff_sources,
                    agent_variable, checkpointer=None, resource_tools=()):
    provider = getattr(source, "ksadk_a2a_local_nodes", None)
    if provider is not None:
        if not callable(provider):
            raise ValueError("A2A_LOCAL_NODE_PROVIDER_INVALID")
        nodes = provider()
        if not isinstance(nodes, dict) or set(nodes) != set(local_ids):
            raise ValueError("A2A_LOCAL_NODE_MAPPING_INVALID")
        nodes = dict(nodes)
    elif len(local_ids) == 1:
        nodes = {local_ids[0]: getattr(source, agent_variable)}
    elif local_ids:
        raise ValueError("A2A_LOCAL_NODE_PROVIDER_REQUIRED")
    else:
        nodes = {}
    factories = getattr(source, "ksadk_a2a_local_factories", {})
    if not isinstance(factories, dict):
        raise ValueError("A2A_LOCAL_TOOL_FACTORY_REQUIRED")
    factories = dict(factories)
    if framework == "langgraph":
        factory = getattr(source, "ksadk_graph_factory", None)
        if len(local_ids) == 1 and callable(factory) and provider is None:
            local_id = local_ids[0]
            # Nested graphs inherit the outer saver; never keep a private
            # MemorySaver inside a graph that claims durable recovery.
            if local_id in handoff_sources and local_id not in factories:
                import inspect
                if "resource_tools" not in inspect.signature(factory).parameters:
                    raise ValueError("A2A_LOCAL_TOOL_FACTORY_REQUIRED")
                factories[local_id] = lambda tools: factory(
                    checkpointer=None, resource_tools=tuple(resource_tools) + tuple(tools))
            elif local_id not in handoff_sources:
                if resource_tools:
                    nodes[local_id] = factory(checkpointer=None, resource_tools=tuple(resource_tools))
                else:
                    nodes[local_id] = factory(checkpointer=None)
        elif resource_tools:
            raise ValueError("A2A_RESOURCE_TOOL_FACTORY_REQUIRED")
        for local_id, node in nodes.items():
            if local_id not in handoff_sources and getattr(node, "checkpointer", None) is not None:
                raise ValueError("A2A_LOCAL_CHECKPOINTER_MUST_INHERIT")
        return compose(nodes, local_factories=factories, checkpointer=checkpointer)
    if resource_tools:
        raise ValueError("A2A_RESOURCE_TOOL_FACTORY_REQUIRED")
    return compose(nodes)


def prepare_composed_state(source, payload, session_context):
    hook = getattr(source, "ksadk_prepare_state", None)
    if callable(hook):
        return hook(payload, session_context)
    # The default MessagesState contract admits the current textual turn only.
    # Non-text/custom state requires the project's explicit projection hook.
    from langchain_core.messages import HumanMessage
    current = payload.get("input")
    if not isinstance(current, str):
        raise ValueError("A2A_PREPARE_STATE_HOOK_REQUIRED")
    return {"messages": [HumanMessage(content=current)]}
