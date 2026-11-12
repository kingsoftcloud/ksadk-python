"""Local stdio MCP business fixture; real initialize/call_tool, no API stubs."""

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("a2ui-local-business")


@mcp.tool()
def complete_request(
    answer: dict[str, Any], field: str, surface_id: str, evidence: str
) -> dict[str, Any]:
    """Complete a synthetic request and return declared A2UI operations."""
    with Path(evidence).open("a") as output:
        output.write(
            json.dumps({"phase": "mcp", "answer": answer, "surface_id": surface_id}) + "\n"
        )
    return {
        "a2ui_operations": [
            {
                "version": "v0.9.1",
                "updateComponents": {
                    "surfaceId": surface_id,
                    "components": [
                        {
                            "id": "root",
                            "component": "Text",
                            "text": "Completed: " + str(answer[field]),
                        }
                    ],
                },
            }
        ]
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
