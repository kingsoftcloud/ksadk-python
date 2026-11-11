"""Explicit LangGraph input helper; the native interrupt engine owns execution."""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any

from pydantic import AfterValidator, JsonValue, WithJsonSchema

from ksadk.interaction.langgraph_interrupt import _native_input_schema
from ksadk.interaction.schema import require_json, validate_answer, validate_schema


def request_input(
    prompt: str,
    schema: dict[str, Any],
    initial_values: JsonValue = None,
    presentation: dict[str, Any] | None = None,
) -> JsonValue:
    """Pause the current node and return its validated JSON answer.

    Call inside a native node compiled with a checkpointer. The SDK does not
    choose business parameters or invoke tools. Initial values are display
    suggestions; they never fill omitted fields in a submitted answer.
    """
    from langgraph.types import interrupt

    checked = validate_schema(schema)
    value: dict[str, Any] = {
        "kind": "structured_input",
        "prompt": prompt,
        "schema": checked,
    }
    if initial_values is not None:
        value["initial_values"] = deepcopy(require_json(initial_values))
    if presentation is not None:
        value["presentation"] = deepcopy(require_json(presentation))

    def validate(value: JsonValue) -> JsonValue:
        return validate_answer(checked, value)

    # A raw dict response_schema is descriptive in LangGraph. A native typed
    # validator must reject BEFORE LangGraph commits the resume value, otherwise
    # a corrected answer would replay the already persisted invalid value.
    response_type = Annotated[
        JsonValue,
        AfterValidator(validate),
        WithJsonSchema(_native_input_schema(checked)),
    ]
    return interrupt(value, response_schema=response_type)


__all__ = ["request_input"]
