#!/usr/bin/env python3
"""Tiny dependency-free MCP stdio server used by the real App Server E2E."""

from __future__ import annotations

import json
import sys
from typing import Any


def _result(request_id: object, result: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    request_id = request.get("id")
    if request_id is None:
        continue
    method = request.get("method")
    if method == "initialize":
        _result(
            request_id,
            {
                "protocolVersion": request.get("params", {}).get(
                    "protocolVersion", "2025-06-18"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ksadk-plugin-fixture", "version": "1.0.0"},
            },
        )
    elif method == "ping":
        _result(request_id, {})
    elif method == "tools/list":
        _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "echo_fixture",
                        "description": "Echo one value through the installed plugin MCP server.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
    elif method == "tools/call":
        value = str(request.get("params", {}).get("arguments", {}).get("value", ""))
        _result(
            request_id,
            {
                "content": [{"type": "text", "text": f"plugin-echo:{value}"}],
                "structuredContent": {"echo": value},
                "isError": False,
            },
        )
    else:
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found"},
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
