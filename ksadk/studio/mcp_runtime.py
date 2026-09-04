"""Official MCP SDK adapter for local stdio / streamable-http / SSE probe and Tool execution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.client.streamable_http import streamablehttp_client

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
        self._validate(server)
        try:
            with anyio.fail_after(timeout_seconds):
                async with self._session(server) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        result = await session.list_tools()
        except StudioError:
            raise
        except Exception as exc:
            detail = str(exc) or repr(exc)
            if hasattr(exc, "exceptions"):
                detail += " :: " + " | ".join(str(e) or repr(e) for e in exc.exceptions)
            raise StudioError(
                "MCP_PROBE_FAILED",
                "MCP Server 探测失败",
                status_code=422,
                details={"server": server.name, "errorType": type(exc).__name__, "detail": detail},
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
        self._validate(server)
        try:
            async with self._session(server) as (read_stream, write_stream):
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

    @asynccontextmanager
    async def _session(
        self,
        server: MCPServerRef,
    ) -> AsyncIterator[tuple[Any, Any]]:
        transport = (server.transport or "stdio").lower()
        if transport == "stdio":
            params = self._stdio_params(server)
            async with stdio_client(params) as (read_stream, write_stream):
                yield read_stream, write_stream
        elif transport in {"http", "streamable-http", "streamable_http"}:
            headers = self._http_headers(server)
            async with streamablehttp_client(
                server.endpoint_url or "", headers=headers
            ) as (read_stream, write_stream, _):
                yield read_stream, write_stream
        elif transport == "sse":
            headers = self._http_headers(server)
            async with sse_client(server.endpoint_url or "", headers=headers) as (
                read_stream,
                write_stream,
            ):
                yield read_stream, write_stream
        else:
            raise StudioError(
                "MCP_TRANSPORT_UNSUPPORTED",
                f"不支持的 MCP transport: {transport}",
                status_code=501,
                details={"transport": transport},
            )

    def _stdio_params(self, server: MCPServerRef) -> StdioServerParameters:
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

    def _http_headers(self, server: MCPServerRef) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, reference in (server.env_refs or {}).items():
            try:
                value = self.credentials.resolve(reference)
            except StudioError as exc:
                raise StudioError(
                    "MCP_CREDENTIAL_MISSING",
                    f"MCP Server 引用的凭证未配置：{reference}（请先保存该环境变量的值）",
                    status_code=422,
                    details={"server": server.name, "reference": reference},
                ) from exc
            if name.lower() == "authorization":
                if not value.lower().startswith(("bearer ", "basic ")):
                    value = f"Bearer {value}"
            headers[name] = value
        return headers

    @staticmethod
    def _validate(server: MCPServerRef) -> None:
        if server.materialization == "dsh-profile":
            raise StudioError(
                "DSH_MCP_MANAGED_RESOURCE_REQUIRED",
                "DSH Profile MCP 必须通过受管理的 PluginHost 租约使用",
                status_code=422,
            )
        transport = (server.transport or "stdio").lower()
        if transport == "stdio":
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
        elif transport in {"http", "streamable-http", "streamable_http", "sse"}:
            if not server.endpoint_url:
                raise StudioError(
                    "MCP_TRANSPORT_UNSUPPORTED",
                    "HTTP/SSE MCP 必须配置 endpointUrl",
                    status_code=422,
                    details={"transport": transport},
                )
        else:
            raise StudioError(
                "MCP_TRANSPORT_UNSUPPORTED",
                f"不支持的 MCP transport: {transport}",
                status_code=501,
                details={"transport": transport},
            )
