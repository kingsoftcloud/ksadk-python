"""Generated framework source for Studio quick/conversation authoring flows."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from ksadk.studio.contracts import AgentDraft
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.workspace import Workspace
from ksadk.toolsets import describe_agentengine_tools

_GENERATED_MARKER = ".agentkit-generated"


def materialize_generated_runtime_source(
    workspace: Workspace,
    draft: AgentDraft,
    *,
    catalog: LocalResourceCatalog | None = None,
) -> None:
    """Create or refresh the source owned by a generated ADK/LangGraph draft.

    Imported and detected projects are never overwritten: the marker is created
    only when Studio owns a previously empty source directory.
    """

    runtime = draft.spec.runtime
    # Only the two legacy Python-framework runtimes own generated source.
    # Harness and externally installed AgentProviders are bundle compositions;
    # treating either as the ``else`` branch here would silently generate a
    # LangGraph project and change the selected provider's execution model.
    if (
        runtime is None
        or runtime.type not in {"adk", "langgraph"}
        or not runtime.project_path
    ):
        return
    root = workspace.resolve(runtime.project_path)
    marker = root / _GENERATED_MARKER
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        return
    root.mkdir(parents=True, exist_ok=True)
    entry = workspace.resolve(Path(runtime.project_path) / (runtime.entry_point or "agent.py"))
    entry.parent.mkdir(parents=True, exist_ok=True)
    resolved_catalog = catalog or LocalResourceCatalog(workspace)
    prompt, tool_names, python_tools = _runtime_inputs(
        workspace,
        draft,
        catalog=resolved_catalog,
    )
    _snapshot_python_tools(workspace, root, python_tools)
    model = _configured_model(resolved_catalog, draft)
    if runtime.type == "adk":
        source = _adk_source(
            draft.metadata.id,
            prompt,
            model,
            runtime.agent_variable,
            tool_names,
            python_tools,
        )
    else:
        source = _langgraph_source(
            prompt,
            model,
            runtime.agent_variable,
            tool_names,
            python_tools,
        )
    workspace.atomic_write_text(entry, source)
    workspace.atomic_write_yaml(
        root / "ksadk.yaml",
        {
            "name": draft.metadata.id,
            "framework": runtime.type,
            "entry_point": runtime.entry_point or "agent.py",
            "agent_variable": runtime.agent_variable,
        },
    )
    workspace.atomic_write_text(
        marker,
        "AgentKit Studio generated source. Safe to refresh from its YAML Revision.\n",
    )


def _configured_model(catalog: LocalResourceCatalog, draft: AgentDraft) -> str:
    resolved = catalog.resolve_model(draft.spec.bindings)
    if resolved is not None:
        return resolved.model
    if draft.spec.model is not None:
        return draft.spec.model.model
    return str(draft.metadata.labels.get("agentkit.ksyun.com/model") or "glm-5.1")


def _runtime_inputs(
    workspace: Workspace,
    draft: AgentDraft,
    *,
    catalog: LocalResourceCatalog,
) -> tuple[str, list[str], list[dict[str, str]]]:
    tools, _permissions = catalog.policy_preview(draft.spec.bindings)
    builtin_names = [tool.name for tool in tools if tool.enabled and tool.executor == "builtin"]
    python_tools = [
        {
            "name": tool.name,
            "description": tool.description,
            "sourcePath": str(tool.source_path),
            "callableName": str(tool.callable_name),
            "sourceSha256": str(tool.source_sha256),
            "bundlePath": f".agentkit_tools/{_python_name(tool.name)}.py",
        }
        for tool in tools
        if tool.enabled and tool.executor == "python"
    ]
    unsupported_executors = sorted(
        {tool.executor for tool in tools if tool.enabled} - {"builtin", "python"}
    )
    if unsupported_executors:
        raise StudioError(
            "TOOL_RUNTIME_INCOMPATIBLE",
            "所选 Tool 类型不能直接注入当前 Python Runtime",
            status_code=422,
            details={"executors": unsupported_executors},
        )
    available = {
        str(item["name"])
        for item in describe_agentengine_tools(profile="coding", mode="direct")
        if item.get("enabled", True)
    }
    unsupported = sorted(set(builtin_names) - available)
    if unsupported:
        raise StudioError(
            "TOOL_RUNTIME_INCOMPATIBLE",
            "所选 Tool 没有可供 Python Runtime 执行的实现",
            status_code=422,
            details={"tools": unsupported},
        )

    prompt = draft.spec.instructions.system.strip() or "You are a reliable assistant."
    skill_sections: list[str] = []
    for ref in catalog.resolve_skills(draft.spec.bindings):
        path = workspace.resolve(
            Path("capabilities/skills") / ref.name / "SKILL.md",
            must_exist=True,
        )
        content = _skill_body(path.read_text(encoding="utf-8-sig"))
        if content:
            skill_sections.append(f"## Skill: {ref.name}\n\n{content}")
    if skill_sections:
        prompt = f"{prompt}\n\n# Bound skills\n\n" + "\n\n".join(skill_sections)
    return prompt, builtin_names, python_tools


def _snapshot_python_tools(
    workspace: Workspace,
    runtime_root: Path,
    tools: list[dict[str, str]],
) -> None:
    target_root = runtime_root / ".agentkit_tools"
    if target_root.exists():
        shutil.rmtree(target_root)
    for tool in tools:
        source = workspace.resolve(tool["sourcePath"], must_exist=True)
        content = source.read_bytes()
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if digest != tool["sourceSha256"]:
            raise StudioError(
                "TOOL_SOURCE_DIGEST_MISMATCH",
                "Python Tool 源码与 Catalog 锁定摘要不一致",
                status_code=409,
                details={"tool": tool["name"]},
            )
        target = runtime_root / tool["bundlePath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _skill_body(content: str) -> str:
    normalized = content.strip()
    if normalized.startswith("---\n"):
        parts = normalized.split("\n---\n", 1)
        if len(parts) == 2:
            normalized = parts[1].strip()
    return normalized


def _python_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not normalized or normalized[0].isdigit():
        normalized = f"agent_{normalized}"
    return normalized


def _adk_source(
    agent_id: str,
    prompt: str,
    model: str,
    variable: str,
    tool_names: list[str],
    python_tools: list[dict[str, str]],
) -> str:
    return f'''"""Generated by AgentKit Studio. Agent behavior remains YAML-owned."""

import os
import importlib.util
from pathlib import Path

from google.adk.agents import Agent
from google.adk.integrations.langchain import LangchainTool
from google.adk.models.lite_llm import LiteLlm
from ksadk.toolsets import get_agentengine_tools
from langchain_core.tools import StructuredTool


def _model_reference() -> str:
    selected = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME") or {json.dumps(model)}
    return selected if "/" in selected else f"openai/{{selected}}"


def _load_python_tools():
    loaded = []
    for index, descriptor in enumerate({json.dumps(python_tools)}):
        path = Path(__file__).parent / descriptor["bundlePath"]
        module_spec = importlib.util.spec_from_file_location(
            f"agentkit_custom_tool_{{index}}",
            path,
        )
        if module_spec is None or module_spec.loader is None:
            raise RuntimeError(f"Cannot load custom tool: {{descriptor['name']}}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        function = getattr(module, descriptor["callableName"])
        loaded.append(
            StructuredTool.from_function(
                function,
                name=descriptor["name"],
                description=descriptor["description"],
            )
        )
    return loaded


_builtin_tool_names = {json.dumps(tool_names)}
_langchain_tools = (
    get_agentengine_tools(
        include=_builtin_tool_names,
        profile="coding",
        mode="direct",
    )
    if _builtin_tool_names
    else []
) + _load_python_tools()
_tools = [LangchainTool(tool) for tool in _langchain_tools]


{variable} = Agent(
    name={json.dumps(_python_name(agent_id))},
    model=LiteLlm(model=_model_reference()),
    instruction={json.dumps(prompt)},
    tools=_tools,
)
'''


def _langgraph_source(
    prompt: str,
    model: str,
    variable: str,
    tool_names: list[str],
    python_tools: list[dict[str, str]],
) -> str:
    return f'''"""Generated by AgentKit Studio. Agent behavior remains YAML-owned."""

import os
import importlib.util
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from ksadk.toolsets import get_agentengine_tools
from langchain_core.tools import StructuredTool


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _load_python_tools():
    loaded = []
    for index, descriptor in enumerate({json.dumps(python_tools)}):
        path = Path(__file__).parent / descriptor["bundlePath"]
        module_spec = importlib.util.spec_from_file_location(
            f"agentkit_custom_tool_{{index}}",
            path,
        )
        if module_spec is None or module_spec.loader is None:
            raise RuntimeError(f"Cannot load custom tool: {{descriptor['name']}}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        function = getattr(module, descriptor["callableName"])
        loaded.append(
            StructuredTool.from_function(
                function,
                name=descriptor["name"],
                description=descriptor["description"],
            )
        )
    return loaded


_builtin_tool_names = {json.dumps(tool_names)}
_tools = (
    get_agentengine_tools(
        include=_builtin_tool_names,
        profile="coding",
        mode="direct",
    )
    if _builtin_tool_names
    else []
) + _load_python_tools()


def _call_model(state: AgentState):
    selected = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME") or {json.dumps(model)}
    client = ChatOpenAI(
        model=selected,
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE"),
        stream_usage=True,
    )
    runnable = client.bind_tools(_tools) if _tools else client
    messages = list(state["messages"])
    # KsADK Runtime 已投影 CompiledPrompt/ContextPlan 时，state 中存在 SystemMessage，
    # 不再重复注入模板 Prompt；直接调用 graph 时仍保留独立运行能力。
    if not any(isinstance(message, SystemMessage) for message in messages):
        messages.insert(0, SystemMessage(content={json.dumps(prompt)}))
    return {{"messages": [runnable.invoke(messages)]}}


def ksadk_graph_factory(*, checkpointer):
    """Compile the graph with a caller-owned checkpoint backend.

    Studio uses ``MemorySaver`` for local authoring.  The hosted KsADK runner
    rebuilds this graph through the same factory with its admitted PostgreSQL
    saver before the first turn, so an interrupt can resume after a Pod move.
    """
    builder = StateGraph(AgentState)
    builder.add_node("model", _call_model)
    builder.add_edge(START, "model")
    if _tools:
        builder.add_node("tools", ToolNode(_tools))
        builder.add_conditional_edges("model", tools_condition)
        builder.add_edge("tools", "model")
    else:
        builder.add_edge("model", END)
    return builder.compile(checkpointer=checkpointer)


{variable} = ksadk_graph_factory(checkpointer=MemorySaver())
'''


__all__ = ["materialize_generated_runtime_source"]
