"""Dispatch bound native tools and recover only through their owning ports."""

from __future__ import annotations

from collections.abc import Mapping

from ksadk.harness.native.ports import ToolPort, ToolRequest, ToolResult


class NativeToolMux:
    def __init__(self, ports: Mapping[str, ToolPort]) -> None:
        self._ports = dict(ports)

    async def execute(self, request: ToolRequest) -> ToolResult:
        port = self._ports.get(request.call.name)
        if port is None:
            return ToolResult(status="error", content={"code": "unknown_tool"})
        return await port.execute(request)

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        from ksadk.harness.native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(self._ports.get(request.call.name), request)

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        from ksadk.harness.native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self._ports.get(request.call.name), request, method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        from ksadk.harness.native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self._ports.get(request.call.name), request, method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        from ksadk.harness.native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self._ports.get(request.call.name), request, method="resume_checkpoint"
        )
