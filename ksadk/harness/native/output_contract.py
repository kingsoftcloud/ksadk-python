"""Validate bounded, host-pinned structured delivery without resolving schemas."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue


def submission_output_format(schema, payload_key=None):
    """The host's schema fixes encoding; the model cannot choose a new format."""
    value = schema.get("properties", {}).get(payload_key) if payload_key else schema
    kind = value.get("type") if isinstance(value, dict) else None
    if kind == "object":
        return "json_object"
    if payload_key and kind == "string":
        return "text"
    if (
        payload_key and isinstance(kind, str)
        and kind in {"integer", "number", "boolean", "array", "null"}
    ):
        return "json_value"
    raise ValueError("final submission payload requires one explicit JSON type")


def object_output_schema(schema, payload_key=None):
    """Detach an already host-validated object's delivery constraint."""
    if not isinstance(schema, dict):
        return None
    value = schema.get("properties", {}).get(payload_key) if payload_key else schema
    if not isinstance(value, dict) or value.get("type") != "object":
        return None
    return json.loads(json.dumps(value, ensure_ascii=False))


def includes_object_constraint(schema, payload_key, required):
    """Only exact or conjunctive inclusion; never infer schema implication."""
    actual = object_output_schema(schema, payload_key)
    return actual is not None and (
        actual == required
        or (isinstance(actual.get("allOf"), list) and required in actual["allOf"])
    )


def _has_reference_or_excess_depth(value: JsonValue, *, depth: int = 0) -> bool:
    if depth > 32:
        return True
    if isinstance(value, dict):
        if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
            return True
        return any(_has_reference_or_excess_depth(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_has_reference_or_excess_depth(item, depth=depth + 1) for item in value)
    return False


def validate_submission_schema(
    schema: dict[str, JsonValue], payload_key: str | None = None
) -> None:
    if schema.get("type") != "object":
        raise ValueError("final submission schema must describe an object")
    if _has_reference_or_excess_depth(schema):
        raise ValueError("final submission schema cannot use references or deep nesting")
    if len(json.dumps(schema, sort_keys=True)) > 16384:
        raise ValueError("final submission schema exceeds host limit")
    if payload_key is not None:
        properties = schema.get("properties", {})
        if (
            not isinstance(properties, dict)
            or not isinstance(properties.get(payload_key), dict)
            or payload_key not in schema.get("required", [])
        ):
            raise ValueError("final submission payload must be a required typed property")
    submission_output_format(schema, payload_key)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("invalid final submission schema") from exc
