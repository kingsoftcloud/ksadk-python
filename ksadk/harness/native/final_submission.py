"""Host-governed structured final submission, separate from executable tools."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .model import ModelToolCall
from .ports import RunConfiguration, ToolDefinition

FINAL_SUBMISSION_TOOL = "submit_final_output"


def final_submission_tool(config: RunConfiguration) -> ToolDefinition | None:
    schema = config.final_submission_schema
    if schema is None:
        return None
    return ToolDefinition(
        name=FINAL_SUBMISSION_TOOL,
        description=(
            "Submit the final structured answer for host verification. "
            "This does not execute a workspace action."
            + (
                (" Put the complete requested JSON object in "
                 f"{config.final_submission_payload_key}."
                 if config.final_output_format == "json_object"
                 else " Put only the complete requested value in "
                 f"{config.final_submission_payload_key}.")
                if config.final_submission_payload_key is not None
                else ""
            )
        ),
        parameters=schema,
    )


def submitted_final_text(call: ModelToolCall, config: RunConfiguration) -> str | None:
    schema = config.final_submission_schema
    if schema is None or call.name != FINAL_SUBMISSION_TOOL or call.wire_arguments_invalid:
        return None
    try:
        Draft202012Validator(schema).validate(call.arguments)
    except ValidationError:
        return None
    output = (
        call.arguments[config.final_submission_payload_key]
        if config.final_submission_payload_key is not None
        else call.arguments
    )
    if config.final_output_format == "text":
        return output if isinstance(output, str) else None
    try:
        return json.dumps(output, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        return None
