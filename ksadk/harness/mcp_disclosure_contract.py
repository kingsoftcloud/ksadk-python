"""Framework-free L0-L3 MCP tool contract shared by runtime implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MCP_LIST_TOOLS_TOOL = "mcp_list_tools"
MCP_READ_SCHEMA_TOOL = "mcp_read_tool_schema"
MCP_CALL_TOOL_TOOL = "mcp_call_tool"


@dataclass(frozen=True)
class McpDisclosureToolSpec:
    """暴露给模型的受控 MCP 披露工具描述。"""

    name: str
    description: str
    parameters: dict[str, Any]

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


def disclosure_tools() -> list[McpDisclosureToolSpec]:
    server_id = {"type": "string", "description": "Revision 已绑定的 MCP Server 引用。"}
    tool_name = {"type": "string", "description": "该 Server 上的 Tool 名。"}
    return [
        McpDisclosureToolSpec(
            name=MCP_LIST_TOOLS_TOOL,
            description=(
                "列出指定 MCP Server 的可用 Tool（名称与一句话描述，不含参数 Schema）。"
                "使用该 Server 的任何 Tool 前必须先调用此工具。refresh=true 时刷新缓存。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": server_id,
                    "refresh": {"type": "boolean", "description": "失效并重新拉取工具列表"},
                },
                "required": ["server_id"],
                "additionalProperties": False,
            },
        ),
        McpDisclosureToolSpec(
            name=MCP_READ_SCHEMA_TOOL,
            description=(
                "读取指定 Tool 的完整输入参数 Schema。必须先 mcp_list_tools 该 Server，"
                "且调用任何 Tool 前必须先读其 Schema。"
            ),
            parameters={
                "type": "object",
                "properties": {"server_id": server_id, "tool_name": tool_name},
                "required": ["server_id", "tool_name"],
                "additionalProperties": False,
            },
        ),
        McpDisclosureToolSpec(
            name=MCP_CALL_TOOL_TOOL,
            description=(
                "调用指定 MCP Tool。必须先用 mcp_read_tool_schema 读取该 Tool 的 Schema。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_id": server_id,
                    "tool_name": tool_name,
                    "arguments": {
                        "type": "object",
                        "description": "按 Schema 组装的工具参数。",
                    },
                },
                "required": ["server_id", "tool_name", "arguments"],
                "additionalProperties": False,
            },
        ),
    ]
