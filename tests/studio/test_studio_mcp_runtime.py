from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ksadk.studio.contracts import MCPServerRef
from ksadk.studio.errors import StudioError
from ksadk.studio.mcp_runtime import MCPRuntimeAdapter
from ksadk.studio.workspace import Workspace


def _server(tmp_path: Path) -> tuple[Workspace, MCPServerRef]:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    script = workspace.root / "demo_mcp_server.py"
    script.write_text(
        """
from mcp.server.fastmcp import FastMCP

server = FastMCP("AgentKit Test MCP")

@server.tool()
def mcp_echo(value: str) -> str:
    \"\"\"Echo a value.\"\"\"
    return value

if __name__ == "__main__":
    server.run(transport="stdio")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return workspace, MCPServerRef(
        name="demo-mcp",
        version="1.0.0",
        transport="stdio",
        command=sys.executable,
        args=[str(script)],
    )


@pytest.mark.asyncio
async def test_official_mcp_adapter_probes_and_calls_stdio_server(tmp_path: Path):
    workspace, server = _server(tmp_path)
    adapter = MCPRuntimeAdapter(workspace)

    probed = await adapter.probe(server, timeout_seconds=10)
    called = await adapter.call(
        server,
        tool_name="mcp_echo",
        arguments={"value": "hello"},
        timeout_seconds=10,
    )

    assert probed["serverInfo"]["name"] == "AgentKit Test MCP"
    assert probed["tools"][0]["name"] == "mcp_echo"
    assert probed["tools"][0]["executor"] == "mcp"
    assert probed["tools"][0]["mcpServer"] == "demo-mcp"
    assert called["isError"] is False
    assert called["content"][0]["text"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("sh", ["server.sh"]),
        ("python", ["-c", "print('unsafe')"]),
        ("node", ["--eval", "console.log('unsafe')"]),
    ],
)
async def test_mcp_adapter_rejects_shell_and_inline_eval(
    tmp_path: Path,
    command: str,
    args: list[str],
):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    server = MCPServerRef(
        name="unsafe-mcp",
        version="1.0.0",
        transport="stdio",
        command=command,
        args=args,
    )

    with pytest.raises(StudioError) as captured:
        await MCPRuntimeAdapter(workspace).probe(server)

    assert captured.value.code == "MCP_COMMAND_DENIED"
