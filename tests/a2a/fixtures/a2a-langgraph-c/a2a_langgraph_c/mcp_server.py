"""Read-only deterministic MCP server for A2A acceptance, using real stdio transport."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("a2a-acceptance")

@server.tool()
def echo(text: str) -> str:
    """Return the supplied acceptance marker without side effects."""
    return "mcp:" + text

if __name__ == "__main__":
    server.run(transport="stdio")
