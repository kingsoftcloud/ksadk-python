"""Typed, JSON-serializable content values for canonical runtime events."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextContent(_ContentModel):
    content_type: Literal["text"] = "text"
    part_id: str = Field(min_length=1)
    text: str


class JsonContent(_ContentModel):
    content_type: Literal["json"] = "json"
    part_id: str = Field(min_length=1)
    value: JsonValue


class ToolCallContent(_ContentModel):
    content_type: Literal["tool_call"] = "tool_call"
    part_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: JsonValue


class ToolResultContent(_ContentModel):
    content_type: Literal["tool_result"] = "tool_result"
    part_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    result: JsonValue
    is_error: bool = False


class ArtifactContent(_ContentModel):
    content_type: Literal["artifact"] = "artifact"
    part_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mime_type: str | None = None
    uri: str | None = None
    data: JsonValue = None


class DataContent(_ContentModel):
    content_type: Literal["data"] = "data"
    part_id: str = Field(min_length=1)
    data: JsonValue


ContentValue: TypeAlias = Annotated[
    Union[
        TextContent,
        JsonContent,
        ToolCallContent,
        ToolResultContent,
        ArtifactContent,
        DataContent,
    ],
    Field(discriminator="content_type"),
]

# An update carries one named part. ``op`` on ItemUpdated defines whether that
# part is appended or replaced; snapshots carry the complete ordered part set.
ContentUpdate: TypeAlias = ContentValue


class ContentSnapshot(_ContentModel):
    parts: tuple[ContentValue, ...] = Field(strict=False)


__all__ = [
    "ArtifactContent",
    "ContentSnapshot",
    "ContentUpdate",
    "ContentValue",
    "DataContent",
    "JsonContent",
    "TextContent",
    "ToolCallContent",
    "ToolResultContent",
]
