"""Borrow an activation's exact MCP tool surface without owning its transports.

Remote tool effects stay on the original bound resources. A child's local
workspace isolation cannot isolate those resources or make calls replay-safe.
The original surface owns schema, Scope, approval and activation checks.
"""

from ksadk.harness.native.ports import RunConfiguration, ToolDefinition, ToolPort, ToolResult
from ksadk.harness.native.state import RunScope
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.harness.native.subagents import ChildRunRequest
from ksadk.studio.native_mcp_tool_port import NativeMcpToolSurface


class _BorrowedChildMcpTools:
    def __init__(self, *, child, names, executor, tools):
        self.child, self.names, self.executor, self.tools = child, frozenset(names), executor, tools

    async def execute(self, request):
        if request.scope != self.child.scope or request.run_id != self.child.child_run_id:
            return ToolResult(status="error", content={"code": "child_mcp_run_binding_mismatch"})
        if request.call.name in self.names:
            return await self.executor.execute(request)
        if self.tools is None:
            return ToolResult(status="error", content={"code": "child_tool_not_granted"})
        return await self.tools.execute(request)


class StudioChildMcp:
    def __init__(
        self,
        *,
        scope: RunScope,
        surface: NativeMcpToolSurface,
        activation_binding_digest: str,
    ) -> None:
        self.scope = scope
        self.definitions = tuple(
            ToolDefinition.model_validate_json(item.model_dump_json())
            for item in surface.definitions
        )
        self.tool_names = tuple(item.name for item in self.definitions)
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("child_mcp_tool_name_duplicate")
        self.executor = surface.executor
        self.binding_digest = studio_mapping_digest(
            {
                "policy": "studio-child-borrowed-mcp/v1",
                "activation_binding": activation_binding_digest,
                "definitions": [item.model_dump(mode="json") for item in self.definitions],
            }
        )

    def bind(
        self,
        child: ChildRunRequest,
        configuration: RunConfiguration,
        tools: ToolPort | None,
    ) -> tuple[RunConfiguration, ToolPort]:
        if child.parent_scope != self.scope or child.scope != self.scope:
            raise ValueError("child_mcp_binding_scope_mismatch")
        if not set(self.tool_names).issubset(child.role.tool_names):
            raise ValueError("child_mcp_role_grant_mismatch")
        if child.role.concurrent_safe and any(not item.read_only for item in self.definitions):
            raise ValueError("child_mcp_concurrent_effects_unsupported")
        by_name = {item.name: item for item in configuration.tools}
        for item in self.definitions:
            if item.name in by_name:
                raise ValueError("child_mcp_tool_name_conflict")
        configuration = RunConfiguration.model_validate(
            {
                **configuration.model_dump(),
                "tools": (*configuration.tools, *self.definitions),
                "host_binding_digest": studio_mapping_digest(
                    {
                        "parent_binding": configuration.host_binding_digest,
                        "inherited_mcp_binding": self.binding_digest,
                    }
                ),
            }
        )
        return configuration, _BorrowedChildMcpTools(
            child=child,
            names=self.tool_names,
            executor=self.executor,
            tools=tools,
        )
