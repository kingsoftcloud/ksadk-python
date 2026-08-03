"""Official MCP SDK adapter for local stdio probe and Tool execution."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)

from ksadk.studio.contracts import MCPServerRef, ToolContract
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import CredentialResolver
from ksadk.studio.workspace import Workspace

_DENIED_COMMANDS = {"bash", "cmd", "powershell", "pwsh", "sh", "zsh"}
_DENIED_EVAL_ARGS = {"-c", "-e", "--eval"}


class MCPRuntimeAdapter:
    def __init__(
        self,
        workspace: Workspace,
        *,
        credentials: CredentialResolver | None = None,
    ) -> None:
        self.workspace = workspace
        self.credentials = credentials or CredentialResolver()

    async def probe(
        self,
        server: MCPServerRef,
        *,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        self._require_stdio(server)
        params = self._parameters(server)
        try:
            with anyio.fail_after(timeout_seconds):
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        result = await session.list_tools()
        except StudioError:
            raise
        except Exception as exc:
            raise StudioError(
                "MCP_PROBE_FAILED",
                "MCP Server 探测失败",
                status_code=422,
                details={"server": server.name, "errorType": type(exc).__name__},
            ) from exc
        tools = [
            ToolContract(
                name=tool.name,
                version=server.version,
                description=tool.description or "",
                input_schema=tool.inputSchema,
                output_schema={"type": "object"},
                executor="mcp",
                mcp_server=server.name,
            ).model_dump(by_alias=True, exclude_none=True, mode="json")
            for tool in result.tools
        ]
        return {
            "serverInfo": initialized.serverInfo.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "tools": tools,
            "timeoutSeconds": timeout_seconds,
        }

    async def call(
        self,
        server: MCPServerRef,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self._require_stdio(server)
        params = self._parameters(server)
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool_name,
                        arguments,
                        read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    )
        except StudioError:
            raise
        except Exception as exc:
            raise StudioError(
                "TOOL_EXECUTION_FAILED",
                "MCP Tool 执行失败",
                status_code=502,
                details={
                    "server": server.name,
                    "tool": tool_name,
                    "errorType": type(exc).__name__,
                },
            ) from exc
        if result.isError:
            raise StudioError(
                "TOOL_EXECUTION_FAILED",
                "MCP Tool 返回错误",
                status_code=502,
                details={"server": server.name, "tool": tool_name},
            )
        return {
            "content": [
                item.model_dump(by_alias=True, exclude_none=True, mode="json")
                for item in result.content
            ],
            "structuredContent": result.structuredContent,
            "isError": False,
        }

    def _parameters(self, server: MCPServerRef) -> StdioServerParameters:
        assert server.command
        environment = get_default_environment()
        for name, reference in server.env_refs.items():
            environment[name] = self.credentials.resolve(reference)
        return StdioServerParameters(
            command=server.command,
            args=server.args,
            env=environment,
            cwd=self.workspace.root,
        )

    @staticmethod
    def _require_stdio(server: MCPServerRef) -> None:
        if server.transport != "stdio":
            raise StudioError(
                "MCP_TRANSPORT_UNSUPPORTED",
                "一期本地 MCP Runtime 仅支持 stdio transport",
                status_code=501,
                details={"transport": server.transport},
            )
        command = Path(server.command or "").name.lower()
        if command in _DENIED_COMMANDS:
            raise StudioError(
                "MCP_COMMAND_DENIED",
                "MCP command 不能直接启动 shell",
                status_code=403,
                details={"command": command},
            )
        if command in {"node", "python", "python3"} and any(
            argument in _DENIED_EVAL_ARGS for argument in server.args
        ):
            raise StudioError(
                "MCP_COMMAND_DENIED",
                "MCP command 不允许使用内联 eval 参数",
                status_code=403,
                details={"command": command},
            )
