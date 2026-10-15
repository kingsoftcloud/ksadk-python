"""Model-authored task planning kept separate from acceptance authority."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .ports import ToolDefinition
from .state import Contract

TODO_WRITE_NAME = "TodoWrite"


class TaskProgressItem(Contract):
    # Optional for old clients. Explicit IDs survive editing/reordering; public
    # projections derive deterministic content IDs for legacy items.
    id: str | None = Field(default=None, min_length=1, max_length=180,
                           exclude_if=lambda value: value is None)
    content: str = Field(min_length=1, max_length=500, pattern=r"\S")
    status: Literal["pending", "in_progress", "completed"]
    priority: Literal["high", "medium", "low"]


class TaskProgressUpdate(Contract):
    todos: tuple[TaskProgressItem, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_content(self) -> TaskProgressUpdate:
        normalized = [item.content.strip().casefold() for item in self.todos]
        if len(set(normalized)) != len(normalized):
            raise ValueError("task progress items must have unique content")
        identities = [item.id for item in self.todos if item.id is not None]
        if len(identities) != len(set(identities)):
            raise ValueError("task progress items must have unique ids")
        return self


TODO_WRITE_DEFINITION = ToolDefinition(
    name=TODO_WRITE_NAME,
    context_role="task_planning",
    description=(
        "Replace the current session task list used to track multi-step work. "
        "Send the complete list. Mark completed only after the work and required validation; "
        "this planning status never substitutes for task acceptance."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["todos"],
        "properties": {
            "todos": {
                "type": "array",
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["content", "status", "priority"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 180},
                        "content": {"type": "string", "minLength": 1, "maxLength": 500},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                },
            }
        },
    },
)


def parse_task_progress(arguments: dict[str, JsonValue]) -> tuple[dict[str, JsonValue], ...]:
    update = TaskProgressUpdate.model_validate(arguments)
    return tuple(item.model_dump(mode="json") for item in update.todos)


def summarize_task_progress(items: tuple[dict[str, JsonValue], ...]) -> dict[str, int]:
    return {
        "total": len(items),
        "pending": sum(item["status"] == "pending" for item in items),
        "in_progress": sum(item["status"] == "in_progress" for item in items),
        "completed": sum(item["status"] == "completed" for item in items),
    }
