"""Reserved TodoWrite routing; the reducer commits the validated plan atomically."""

from __future__ import annotations

from ..native.ports import RunConfiguration, ToolPort, ToolRequest, ToolResult
from ..native.task_progress import (
    TODO_WRITE_DEFINITION,
    TODO_WRITE_NAME,
    parse_task_progress,
    summarize_task_progress,
)
from ..native.tool_recovery import query_sealed_tool_result


class NativeTodoToolPort:
    def __init__(self, delegate: ToolPort) -> None:
        if not callable(getattr(delegate, "execute", None)):
            raise ValueError("todo tool delegate must implement execute")
        self.delegate = delegate

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self.delegate, request, owned_names=(TODO_WRITE_NAME,)
        )

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self.delegate, request, owned_names=(TODO_WRITE_NAME,), method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self.delegate, request, owned_names=(TODO_WRITE_NAME,), method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        return await query_sealed_tool_result(
            self.delegate, request, owned_names=(TODO_WRITE_NAME,), method="resume_checkpoint"
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        if request.call.name != TODO_WRITE_NAME:
            result = await self.delegate.execute(request)
            return ToolResult.model_validate_json(result.model_dump_json())
        if request.definition != TODO_WRITE_DEFINITION:
            return ToolResult(status="error", content={"reason": "todo_definition_mismatch"})
        try:
            items = parse_task_progress(request.call.arguments)
        except ValueError:
            return ToolResult(status="error", content={"reason": "invalid_task_progress"})
        return ToolResult(
            status="ok",
            content={
                "todos": list(items),
                "summary": summarize_task_progress(items),
                "authority": "model_planning_only",
            },
        )


def install_todo_tool(
    configuration: RunConfiguration, tools: ToolPort
) -> tuple[RunConfiguration, NativeTodoToolPort]:
    existing = next(
        (definition for definition in configuration.tools if definition.name == TODO_WRITE_NAME),
        None,
    )
    if existing is not None and existing != TODO_WRITE_DEFINITION:
        raise ValueError("reserved_todo_tool_conflict")
    definitions = (
        configuration.tools
        if existing is not None
        else (*configuration.tools, TODO_WRITE_DEFINITION)
    )
    return configuration.model_copy(update={"tools": definitions}), NativeTodoToolPort(tools)
