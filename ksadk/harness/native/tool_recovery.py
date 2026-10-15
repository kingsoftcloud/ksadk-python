"""Host-pinned recovery methods, with no fallback to ordinary tool execution."""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from .ports import ToolPort, ToolRequest, ToolResult


async def query_sealed_tool_result(
    port: ToolPort | None, request: ToolRequest, *, owned_names: Collection[str] = (),
    method: Literal[
        "reconcile", "recover_prepared", "resume_prepared", "resume_checkpoint"
    ] = "reconcile",
) -> ToolResult:
    # A wrapper may forward queries for other host capabilities, but its own
    # tools do not acquire replay authority merely by sharing that wrapper.
    query = getattr(port, method, None)
    if request.call.name in owned_names or not callable(query):
        return ToolResult(
            status="uncertain", content={"error_code": "tool_reconciliation_unavailable"}
        )
    result = await query(request.model_copy(deep=True))
    return ToolResult.model_validate_json(result.model_dump_json())
