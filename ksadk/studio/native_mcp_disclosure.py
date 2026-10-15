"""Native MCP deferred discovery, authorized by each Run's committed receipts."""

from __future__ import annotations

import hashlib
import json
import re

from ksadk.harness.mcp_disclosure_contract import disclosure_tools
from ksadk.harness.native.adapters import ToolBinding
from ksadk.harness.native.effect_admission import check_effect_admission
from ksadk.harness.native.ports import ToolDefinition, ToolResult
from ksadk.harness.native.tool_input_validation import ToolInputValidator

_MODEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
CALL_TOOL = "mcp_call_tool"


def schema_digest(schema):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                schema,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )


class NativeMcpDisclosure:
    def __init__(self, *, runtime, refs, scope, store, available, invoke):
        self.runtime, self.refs, self.scope, self.store = runtime, tuple(refs), scope, store
        self.available, self.invoke = available, invoke
        definitions = []
        for tool in disclosure_tools():
            parameters = json.loads(json.dumps(tool.parameters))
            parameters["properties"]["server_id"]["enum"] = list(self.refs)
            definitions.append(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters=parameters,
                    read_only=tool.name != CALL_TOOL,
                    result_model_policy="typed-tool-observation/v1"
                    if tool.name == CALL_TOOL
                    else None,
                    result_disclosure_policy="mcp-default/v1" if tool.name == CALL_TOOL else None,
                )
            )
        self.definitions = tuple(definitions)
        self.by_name = {tool.name: tool for tool in self.definitions}

    def bindings(self):
        return tuple(ToolBinding(tool, self.execute) for tool in self.definitions)

    def _cursor(self, request, server_id):
        """Rebuild authority from this Run, never inherited model history or text."""
        listed, schemas = False, {}
        for event in self.store.events(self.scope, request.run_id):
            if event.kind != "effect.received" or event.payload.get("kind") != "tool":
                continue
            key = event.payload.get("operation_key")
            intent = self.store.get_intent(self.scope, request.run_id, key)
            receipt = self.store.get_receipt(self.scope, request.run_id, key)
            if (
                intent is None
                or receipt is None
                or receipt.status != "committed"
                or receipt.result.get("status") != "ok"
            ):
                continue
            call = intent.payload.get("call")
            if not isinstance(call, dict) or call.get("name") not in self.by_name:
                continue
            arguments = call.get("arguments", {})
            if arguments.get("server_id") != server_id:
                continue
            if call["name"] == "mcp_list_tools":
                listed, schemas = True, {}
            elif listed and call["name"] == "mcp_read_tool_schema":
                value = receipt.result.get("progress_observation_digest")
                if isinstance(value, str):
                    schemas[arguments.get("tool_name")] = value
        return listed, schemas

    async def _tools(self, server_id, *, refresh=False):
        if not await self.available():
            raise ValueError("activation_unavailable")
        check_effect_admission()
        if refresh:
            self.runtime.invalidate_tools(server_id)
        listed = await self.runtime.tools(server_id)
        binding = self.runtime.binding(server_id)
        allowed = {
            f"{binding.tool_prefix}_{name}" if binding.tool_prefix else name
            for name in binding.static_tool_names
        }
        tools = {}
        for item in listed:
            name = item.get("name")
            if allowed and name not in allowed:
                continue
            if not isinstance(name, str) or not _MODEL_NAME.fullmatch(name) or name in tools:
                raise ValueError("native_mcp_tool_name_invalid_or_duplicate")
            ToolInputValidator(item["inputSchema"])
            tools[name] = item
        if allowed and set(tools) != allowed:
            raise ValueError("native_mcp_locked_tool_missing")
        return tools

    async def authorization_request(self, request):
        """Keep target-name policy while the original wrapper owns the ledger."""
        if request.call.name != CALL_TOOL:
            return request
        arguments = request.call.arguments
        name = arguments["tool_name"]
        schema = {"type": "object"}
        listed, schemas = self._cursor(request, arguments["server_id"])
        if listed and name in schemas:
            try:
                tools = await self._tools(arguments["server_id"])
                tool = tools.get(name)
                if tool is not None and schema_digest(tool["inputSchema"]) == schemas[name]:
                    schema = tool["inputSchema"]
            except Exception:
                pass  # Discovery failure is a definite callback error, not an effect retry.
        return request.model_copy(
            update={
                "admission_call": request.call,
                "call": request.call.model_copy(
                    update={
                        "name": name,
                        "arguments": arguments["arguments"],
                    }
                ),
                "definition": ToolDefinition(
                    name=name,
                    description="Locked MCP target",
                    parameters=schema,
                    result_model_policy="typed-tool-observation/v1",
                    result_disclosure_policy="mcp-default/v1",
                ),
            }
        )

    async def execute(self, request):
        arguments = request.call.arguments
        server_id = arguments.get("server_id")
        if server_id not in self.refs:
            return ToolResult(status="error", content={"code": "mcp_server_not_bound"})
        action = request.call.name
        listed, schemas = self._cursor(request, server_id)
        if action != "mcp_list_tools" and not listed:
            return ToolResult(status="error", content={"code": "mcp_list_required"})
        name = arguments.get("tool_name")
        if action == CALL_TOOL and name not in schemas:
            return ToolResult(status="error", content={"code": "mcp_schema_required"})
        try:
            tools = await self._tools(server_id, refresh=arguments.get("refresh") is True)
        except Exception:
            return ToolResult(status="error", content={"code": "mcp_discovery_unavailable"})
        if action == "mcp_list_tools":
            return ToolResult(
                status="ok",
                content={
                    "server_id": server_id,
                    "tools": [
                        {"name": tool["name"], "description": tool.get("description", "")}
                        for tool in tools.values()
                    ],
                },
            )
        tool = tools.get(name)
        if tool is None:
            return ToolResult(status="error", content={"code": "mcp_tool_not_bound"})
        current = schema_digest(tool["inputSchema"])
        if action == "mcp_read_tool_schema":
            return ToolResult(
                status="ok",
                progress_observation_digest=current,
                content={
                    "server_id": server_id,
                    "tool_name": name,
                    "description": tool.get("description", ""),
                    "input_schema": tool["inputSchema"],
                },
            )
        if schemas[name] != current:
            return ToolResult(status="error", content={"code": "mcp_schema_changed"})
        values = arguments["arguments"]
        if rejection := ToolInputValidator(tool["inputSchema"]).rejection(values):
            return ToolResult(status="error", content=rejection)
        check_effect_admission()
        return await self.invoke(request, server_ref=server_id, tool_name=name, arguments=values)
