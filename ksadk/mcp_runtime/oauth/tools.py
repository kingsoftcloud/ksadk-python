"""OAuth tools for developer-owned LangGraph graphs; never builds or patches a graph."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.graph import MessagesState
from langgraph.types import Command
from pydantic import PrivateAttr

from . import langgraph as oauth
from .client import BrokerError

_CATALOG_ARTIFACT = "ksadk_mcp_oauth"


def _merge_catalogs(current: dict, updates: dict) -> dict:
    # reset() emits an empty update at the start of a new turn.
    return {**current, **updates} if updates else {}


class McpOAuthState(MessagesState):
    """Extend this state (or declare these two fields) in your own graph.

    Only JSON tool definitions enter state. Credentials and live clients do not.
    """

    mcp_catalogs: Annotated[dict[str, list[dict]], _merge_catalogs]
    mcp_stop: bool


def _name(value: str) -> str:
    """Keep ordinary names legible; bound unusual provider names for model APIs."""
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", value) and "__" not in value:
        return value
    slug = re.sub(r"[^a-zA-Z0-9-]", "_", value)[:12].strip("_") or "tool"
    return slug + "_" + hashlib.sha256(value.encode()).hexdigest()[:10]


def _function(name, description, schema):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _pending(state):
    answered = set()
    for message in reversed(state["messages"]):
        if isinstance(message, ToolMessage):
            answered.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            return next((call for call in message.tool_calls if call["id"] not in answered), None)
    return None


def _reply(call, result, *, error=False):
    return ToolMessage(
        name=call["name"],
        tool_call_id=call["id"],
        content=json.dumps(result, ensure_ascii=False),
        status="error" if error else "success",
    )


class _McpOAuthTool(BaseTool):
    """A normal model-bindable tool with a checkpointed graph-state result."""

    _owner: object = PrivateAttr()
    _state: dict = PrivateAttr()

    def __init__(self, owner, state, definition):
        super().__init__(
            name=definition["name"],
            description=definition["description"],
            args_schema=definition["parameters"],
        )
        self._owner, self._state = owner, state

    def _run(self, *args, **kwargs):
        raise NotImplementedError("OAuth MCP tools require async ainvoke with a saved ToolCall")

    async def ainvoke(self, input, config=None, **kwargs):
        # The call was persisted by the model node before this tool executes.
        # Bind its ID/arguments to that node, never accept a new ID on replay.
        from ._durable_tools import saved_call

        pending = saved_call(self._state, input)
        if (
            not isinstance(input, Mapping)
            or not pending
            or input.get("name") != self.name
            or any(input.get(key) != pending.get(key) for key in ("id", "name", "args"))
        ):
            raise BrokerError("MCP_TOOL_CALL_NOT_PENDING")
        # execute already publishes ordinary LangChain tool lifecycle callbacks
        # and preserves GraphInterrupt. Command commits catalog + reply together.
        update = await self._owner._execute_call(self._state, config or {}, pending)
        return Command(update=update)


class McpOAuthTools:
    """Lazy OAuth tools for caller-owned LangGraph graphs.

    Connections may be full name/options declarations (url, description, scopes,
    optional filters/policies/client references), or names from existing YAML.
    Full connections may be constructed inside any running graph node.
    The selected tool binds its declaration to the admitted run on first use.
    Constructing this object and getting tools perform no I/O. Bind get_tools(state)
    in the model node and execute with LangGraph's native ToolNode. Use ordinary
    MessagesState and tools_condition; compile the graph yourself.
    For mixed batches, pass existing tools to get_tools(state, tools=...). Their
    clients/authentication remain unchanged; only the returned tool objects have
    native task receipts.
    With discovery_tools=False, call list_mcp_tools from your own graph node
    and return its JSON catalog in mcp_catalogs. No connector tool is generated.
    execution_policies maps connection/tool names to explicit ToolPolicy values;
    omitted policies never add execution approval based on OAuth alone.
    get_tools(state) returns LangChain tool objects for the current node's state.
    Bind them in the model node and obtain them again in the execution node;
    their Command results save discovery alongside the original ToolMessage.
    """

    def __init__(
        self, servers, *, read_only_tools=None, execution_policies=None, discovery_tools=True
    ):
        if isinstance(servers, Mapping):
            from .code_config import tool_server_declarations

            self.servers, self.declarations = tool_server_declarations(servers)
        elif isinstance(servers, Sequence) and not isinstance(servers, str):
            self.servers = dict.fromkeys(servers, "")
            self.declarations = []
            if len(self.servers) != len(servers):
                raise ValueError("MCP connection names must be unique")
        else:
            raise ValueError("mcp_servers must be connection names or a name/description mapping")
        if any(
            not isinstance(name, str) or not name.strip() or not isinstance(desc, str)
            for name, desc in self.servers.items()
        ):
            raise ValueError("MCP connection names and descriptions must be strings")
        self.read_only = {}
        if read_only_tools is not None and not isinstance(read_only_tools, Mapping):
            raise ValueError("read_only_tools must map connections to tool name lists")
        for connection, names in (read_only_tools or {}).items():
            if (
                connection not in self.servers
                or isinstance(names, str)
                or not isinstance(names, Sequence)
            ):
                raise ValueError(
                    "read_only_tools must map configured connections to tool name lists"
                )
            if any(not isinstance(name, str) or not name for name in names):
                raise ValueError("read_only_tools requires tool names")
            self.read_only[connection] = frozenset(names)
        from ksadk.tools.gateway import ToolPolicy

        self.execution_policies = {}
        if execution_policies is not None and not isinstance(execution_policies, Mapping):
            raise ValueError("execution_policies must map connections to tool policies")
        for connection, policies in (execution_policies or {}).items():
            if connection not in self.servers or not isinstance(policies, Mapping):
                raise ValueError("execution_policies requires configured connections")
            if any(
                not isinstance(name, str) or not name or not isinstance(policy, ToolPolicy)
                for name, policy in policies.items()
            ):
                raise ValueError("execution_policies requires tool names and ToolPolicy values")
            self.execution_policies[connection] = dict(policies)
        # Custom graphs can call list_mcp_tools in their own discovery node.
        # Keep the opt-in agent helper's existing lazy connector mode available.
        self.connectors = (
            {"connect_" + _name(name): name for name in self.servers} if discovery_tools else {}
        )
        if discovery_tools and len(self.connectors) != len(self.servers):
            raise ValueError("MCP connection tool name collision")

    @staticmethod
    def reset(_state):
        """Return updates at the start of a new turn, never during native resume."""
        return {"mcp_catalogs": {}, "mcp_stop": False}

    def _catalog_scope(self):
        # Bind catalog hints to this component's immutable public declarations.
        # A catalog never substitutes for the Broker's per-call authorization.
        value = {"servers": self.servers, "declarations": self.declarations}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _tool_state(self, state):
        state = state if state is not None else {}
        if "mcp_catalogs" in state:
            return state  # Preserve the original explicit-state API.
        catalogs, stopped = {}, False
        turn = []
        for message in reversed(state.get("messages", [])):
            if isinstance(message, HumanMessage):
                break
            turn.append(message)
        scope = self._catalog_scope()
        for message in reversed(turn):
            if not isinstance(message, ToolMessage) or not isinstance(message.artifact, dict):
                continue
            data = message.artifact.get(_CATALOG_ARTIFACT)
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("scope") != scope:
                continue
            catalogs.update(data.get("catalogs", {}))
            stopped = stopped or data.get("stopped", False)
        return {**state, "mcp_catalogs": catalogs, "mcp_stop": stopped}

    async def get_tools(self, state=None, *, tools=()) -> list[BaseTool]:
        """Get this turn's available tools without HTTP, login or model calls.

        Before discovery, return service tools described by the developer. Only
        executing one checks authorization and loads the provider's catalog.
        Afterwards, return real MCP tools with the provider's description/schema.
        Pass current graph state; never cache bound tools across nodes or users.
        Execute the returned tools with native ToolNode. In mixed batches, pass
        the existing tools through tools= so their completed results also survive
        a later OAuth wait. Original tool objects/clients are never modified.
        Each returned batch serializes calls and saves native task receipts; do
        not append unwrapped tools afterwards and expect the same replay guarantee.
        Catalog hints live in ToolMessage artifacts, not model input, and expire at the
        next HumanMessage. Ordinary MessagesState requires no OAuth fields/reset.
        Existing explicit McpOAuthState users retain their previous behavior.
        """
        from ._durable_tools import durable_tools

        state = self._tool_state(state)
        oauth_tools = self._bound_tools(state)
        return durable_tools([*tools, *oauth_tools], state)

    def _bound_tools(self, state):
        return [_McpOAuthTool(self, state, tool["function"]) for tool in self.model_tools(state)]

    def bindings(self, state):
        result = {}
        for connection, catalog in state.get("mcp_catalogs", {}).items():
            if connection not in self.servers:
                continue
            for tool in catalog:
                alias = _name(connection) + "__" + _name(tool["name"])
                if alias in result or alias in self.connectors:
                    raise BrokerError("MCP_TOOL_NAME_COLLISION")
                result[alias] = (connection, tool)
        return result

    def model_tools(self, state):
        """Return model function definitions; SDK fetches schemas only on demand."""
        definitions = []
        for alias, connection in self.connectors.items():
            if connection not in state.get("mcp_catalogs", {}):
                description = self.servers[connection] or f"MCP service {connection}"
                definitions.append(
                    _function(
                        alias,
                        f"Connect on demand and discover tools for {connection}: {description}",
                        {"type": "object", "properties": {}, "additionalProperties": False},
                    )
                )
        for alias, (connection, tool) in self.bindings(state).items():
            definitions.append(
                _function(
                    alias,
                    f"[{connection}] {tool['name']}: {tool['description']}",
                    tool["inputSchema"],
                )
            )
        return definitions

    def owns(self, call, state):
        """Route only these OAuth calls to execute; leave all other tools alone."""
        return bool(
            call and (call["name"] in self.connectors or call["name"] in self.bindings(state))
        )

    async def execute(self, state, config: RunnableConfig):
        """Execute exactly one saved model call, preserving native interrupts.

        Used by the SDK-composed agent graph. One execution per
        checkpoint prevents a later authorization from replaying earlier tools.
        The caller's model node must finish before entering this node.
        """
        return await self._execute_call(state, config, _pending(state))

    async def _execute_call(self, state, config, call):
        if not self.owns(call, state):
            raise ValueError("Tool call is not owned by this OAuth component")
        if state.get("mcp_stop"):
            return self.stop(state)
        # A reused ID inside a turn must not alias an earlier authorization wait.
        ids = []
        for message in reversed(state["messages"]):
            if message.type == "human":
                break
            if isinstance(message, AIMessage):
                ids.extend(c.get("id") for c in message.tool_calls)
        if not call.get("id") or ids.count(call["id"]) != 1:
            raise BrokerError("MCP_MODEL_TOOL_CALL_ID_INVALID")
        connection = self.connectors.get(call["name"])
        binding = self.bindings(state).get(call["name"])
        update = {}

        async def invoke(**arguments):
            try:
                if connection is not None:
                    catalog = await oauth.list_mcp_tools(connection, request_key=call["id"])
                    update["mcp_catalogs"] = {connection: catalog}
                    result = {
                        "connected": connection,
                        "tools": [
                            alias
                            for alias, (name, _) in self.bindings({**state, **update}).items()
                            if name == connection
                        ],
                    }
                else:
                    from ksadk.tools.gateway import ToolPolicy

                    name, tool = binding
                    policy = self.execution_policies.get(name, {}).get(tool["name"])
                    if policy is None and tool["name"] in self.read_only.get(name, ()):
                        policy = ToolPolicy()
                    result = await oauth.call_mcp_tool(
                        name,
                        tool["name"],
                        arguments,
                        request_key=call["id"],
                        policy=policy,
                        raise_on_error=False,
                    )
                return _reply(call, result, error=bool(result.get("isError")))
            except BrokerError as error:
                if error.code == "TOOL_APPROVAL_DENIED":
                    update["mcp_stop"] = True
                return _reply(call, {"error": error.code}, error=True)
            # Never catch GraphInterrupt: Kernel persists and resumes this node.

        schema = (
            {"type": "object", "properties": {}}
            if connection is not None
            else binding[1]["inputSchema"]
        )
        runnable = StructuredTool.from_function(
            coroutine=invoke,
            name=call["name"],
            description="Configured OAuth MCP operation",
            args_schema=schema,
        )
        name = connection if connection is not None else binding[0]
        declaration = next((d for d in self.declarations if d["name"] == name), None)
        with oauth.bind_declaration(declaration):
            reply = await runnable.ainvoke(call, config=config)
        if "mcp_catalogs" in update or update.get("mcp_stop"):
            # Only public JSON schemas are stored. ToolMessage.artifact is not
            # sent to the model; tokens and live clients never enter this data.
            reply = reply.model_copy(
                update={
                    "artifact": {
                        _CATALOG_ARTIFACT: {
                            "version": 1,
                            "scope": self._catalog_scope(),
                            "catalogs": (
                                {connection: update["mcp_catalogs"][connection]}
                                if "mcp_catalogs" in update
                                else {}
                            ),
                            "stopped": bool(update.get("mcp_stop")),
                        }
                    }
                }
            )
        update["messages"] = [reply]
        return update

    @staticmethod
    def stop(state):
        """Close pending tool messages after rejection, without executing them."""
        messages, working = [], dict(state)
        while call := _pending(working):
            reply = _reply(call, {"error": "MCP_OPERATION_STOPPED"}, error=True)
            messages.append(reply)
            working["messages"] = [*working["messages"], reply]
        return {"messages": [*messages, AIMessage(content="已按你的决定停止本次操作。")]}
