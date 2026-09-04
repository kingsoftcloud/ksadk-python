"""Tool catalog for one Harness runner."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ksadk.compat.adk_compat import Agent, InMemorySessionService, InvocationContext, ToolContext
from ksadk.harness.config import McpToolSpec
from ksadk.harness.sandbox import HarnessSandboxExecutor
from ksadk.mcp_runtime import MCPServerConfig, build_mcp_toolset

ToolHandler = Callable[[dict[str, Any], str | None], Awaitable[Any]]

HARNESS_SANDBOX_TOOL_NAMES = frozenset(
    {
        "sandbox_read_file",
        "sandbox_run_command",
    }
)


@dataclass(frozen=True)
class HarnessTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    source: str

    @property
    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def call(self, arguments: dict[str, Any], *, call_id: str | None = None) -> Any:
        return await self.handler(arguments, call_id)


def sandbox_tools(sandbox: HarnessSandboxExecutor) -> list[HarnessTool]:
    async def _read_file(arguments: dict[str, Any], call_id: str | None) -> Any:
        del call_id
        return await sandbox.read_file(str(arguments.get("path") or ""))

    async def _run_command(arguments: dict[str, Any], call_id: str | None) -> Any:
        del call_id
        return await sandbox.run_command(str(arguments.get("command") or ""))

    return [
        HarnessTool(
            name="sandbox_read_file",
            description="Read one UTF-8 file inside the Harness workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_read_file,
            source="sandbox",
        ),
        HarnessTool(
            name="sandbox_run_command",
            description=(
                "Run an allowlisted read-only command inside the Harness workspace. "
                "Writes, network access, and dangerous process actions are denied."
            ),
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=_run_command,
            source="sandbox",
        ),
    ]


async def load_mcp_tools(spec: McpToolSpec) -> tuple[Any, list[HarnessTool]]:
    config = MCPServerConfig(
        name=spec.name,
        url=spec.url,
        api_key=spec.api_key,
        tool_filter=spec.tool_filter,
        tool_name_prefix=spec.tool_name_prefix,
    )
    toolset = build_mcp_toolset(config)
    try:
        native_tools = await toolset.get_tools_with_prefix()
    except BaseException as error:
        cleanup = asyncio.create_task(toolset.close())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        try:
            cleanup.result()
        except Exception:
            pass
        if interrupted or isinstance(error, asyncio.CancelledError):
            raise asyncio.CancelledError
        if not isinstance(error, Exception):
            raise
        raise RuntimeError(
            f"Harness MCP server {spec.name!r} failed to start"
        ) from None

    available_names = {str(tool.name) for tool in native_tools}
    expected_names = {
        f"{spec.tool_name_prefix}_{name}" if spec.tool_name_prefix else name
        for name in spec.tool_filter
    }
    missing_names = sorted(expected_names - available_names)
    if missing_names:
        await toolset.close()
        raise RuntimeError(
            f"Harness MCP server {spec.name!r} did not expose configured "
            f"tool(s): {missing_names}"
        )
    if not native_tools:
        await toolset.close()
        raise RuntimeError(
            f"Harness MCP server {spec.name!r} exposed no callable tools"
        )

    tools: list[HarnessTool] = []
    for native_tool in native_tools:
        # ADK's declaration builder is private and changed across 1.x/2.x.
        # MCP exposes the original public tool schema, which is stable across
        # the supported versions.
        raw_tool = getattr(native_tool, "raw_mcp_tool", None)
        parameters = getattr(raw_tool, "inputSchema", None)
        if parameters is None:
            parameters = getattr(raw_tool, "input_schema", None)
        if parameters is None:
            parameters = getattr(native_tool, "input_schema", None)
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        parameters = _normalize_json_schema(parameters)

        async def _call(
            arguments: dict[str, Any],
            call_id: str | None,
            *,
            _native_tool=native_tool,
            _tool_name=str(native_tool.name),
        ) -> Any:
            try:
                context = await _new_tool_context(call_id or _tool_name)
                result = await _native_tool.run_async(args=arguments, tool_context=context)
                return _normalize_tool_result(result, context)
            except Exception:
                raise RuntimeError(
                    f"Harness MCP tool {_tool_name!r} on server {spec.name!r} failed"
                ) from None

        tools.append(
            HarnessTool(
                name=str(native_tool.name),
                description=str(getattr(native_tool, "description", None) or "MCP tool"),
                parameters=parameters,
                handler=_call,
                source=f"mcp:{spec.name}",
            )
        )
    return toolset, tools


async def _new_tool_context(tool_name: str) -> Any:
    """Build a real ADK ToolContext for a standalone MCP invocation."""
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="ksadk-harness",
        user_id="harness",
        session_id=f"tool-{uuid.uuid4().hex}",
    )
    invocation = InvocationContext(
        session_service=service,
        invocation_id=f"harness-tool-{tool_name}",
        # ADK 1.34 requires an agent while 2.x makes it optional. Supplying a
        # minimal public Agent keeps the same ToolContext construction on both.
        agent=Agent(name="harness_tool", model="gemini-2.0-flash"),
        session=session,
    )
    return ToolContext(invocation_context=invocation, function_call_id=tool_name)


def _normalize_tool_result(result: Any, context: Any) -> Any:
    """Keep MCP failures structured so the reasoner can produce a final answer."""
    confirmations = getattr(context.actions, "requested_tool_confirmations", {})
    if confirmations:
        error = result.get("error") if isinstance(result, dict) else None
        return {
            "ok": False,
            "confirmation_required": True,
            "confirmation_ids": sorted(str(item) for item in confirmations),
            "error": str(error or "tool confirmation is required"),
        }
    if isinstance(result, dict) and result.get("error") is not None:
        return {"ok": False, "error": str(result["error"])}
    return result


def tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and item.get("text") is not None
            ]
            if texts:
                return "\n".join(texts)
    return json.dumps(result, ensure_ascii=False, default=str)


def _normalize_json_schema(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {key: _normalize_json_schema(item) for key, item in value.items()}
        schema_type = normalized.get("type")
        if isinstance(schema_type, str):
            normalized["type"] = schema_type.lower()
        return normalized
    if isinstance(value, list):
        return [_normalize_json_schema(item) for item in value]
    return value


__all__ = [
    "HARNESS_SANDBOX_TOOL_NAMES",
    "HarnessTool",
    "load_mcp_tools",
    "sandbox_tools",
    "tool_result_text",
]
