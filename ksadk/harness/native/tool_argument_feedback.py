"""Bounded schema-derived diagnostics; they never normalize or admit a call."""

from __future__ import annotations

import json
import re

from jsonschema.exceptions import ValidationError
from pydantic import JsonValue


def _pointer(path) -> str:
    return "/" + "/".join(
        str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def _select_failure(error: ValidationError, validator):
    """Narrow only an unambiguously tagged object union using declared consts."""
    selected, relative_path = error, tuple(error.absolute_schema_path)
    while selected.validator in {"oneOf", "anyOf"} and isinstance(selected.instance, dict):
        branches = selected.validator_value
        if not isinstance(branches, list):
            break
        matches = []
        for index, branch in enumerate(branches):
            properties = branch.get("properties", {}) if isinstance(branch, dict) else {}
            if not isinstance(properties, dict):
                continue
            tags = {name: item["const"] for name, item in properties.items()
                    if isinstance(item, dict) and "const" in item}
            if tags and all(
                name in selected.instance
                and validator.evolve(schema={"const": value}).is_valid(selected.instance[name])
                for name, value in tags.items()
            ):
                matches.append(index)
        if len(matches) != 1:
            break  # Unknown/ambiguous tags must not produce invented repair facts.
        failures = [child for child in selected.context
                    if child.schema_path and child.schema_path[0] == matches[0]]
        if not failures:
            break
        selected = next(
            (child for child in failures if child.validator == "type" and child.path),
            failures[0],
        )
        # Retain the existing branch-relative diagnostic for action consumers.
        # The original ValidationError parent chain retains the true origin.
        relative_path = tuple(selected.schema_path)[1:]
    return selected, relative_path


def _physical_schema_path(schema: dict, selected: ValidationError) -> str | None:
    value = schema
    path = tuple(selected.absolute_schema_path)
    try:
        for part in path:
            value = value[part]
        # Local $ref validation may report a resolved rather than physical
        # path. Only advertise an origin that actually exists in the definition.
        if json.dumps(value, sort_keys=True) == json.dumps(
            selected.validator_value, sort_keys=True
        ):
            pointer = _pointer(path)
            return pointer if len(pointer) <= 2048 else None
    except (KeyError, IndexError, TypeError):
        pass
    return None


_JSON_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


def _union_type_hint(error: ValidationError) -> list[str] | None:
    """Describe only pure type alternatives, never enum values or other failures."""

    def types(value):
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values or any(
            not isinstance(item, str) or item not in _JSON_TYPES for item in values
        ):
            return None
        return values

    if error.validator != "anyOf" or not isinstance(error.validator_value, list):
        return None
    branches = error.validator_value
    if not branches or len(error.context) != len(branches) or any(
        child.validator != "type" or child.absolute_path != error.absolute_path
        for child in error.context
    ):
        return None
    result = []
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != {"type"}:
            return None
        allowed = types(branch["type"])
        if allowed is None:
            return None
        result.extend(item for item in allowed if item not in result)
    return result


def _declared_names(schema_path) -> list[str]:
    path, names, index = list(schema_path), [], 0
    while index < len(path):
        if path[index] == "properties" and index + 1 < len(path) and isinstance(
            path[index + 1], str
        ):
            names.append(path[index + 1])
            index += 2
        else:
            index += 1
    return names


def _declared_instance_path(selected: ValidationError) -> bool:
    """Instance keys may be private map keys; repeat only host-declared names."""
    keys = [part for part in selected.absolute_path if isinstance(part, str)]
    return keys == _declared_names(selected.absolute_schema_path)


def _schema_field(selected: ValidationError) -> str | None:
    path = list(selected.absolute_schema_path)
    if len(path) >= 3 and path[-3] == "properties" and isinstance(path[-2], str):
        return path[-2]
    return None


def argument_error(error: ValidationError, schema: dict, validator) -> dict[str, JsonValue]:
    selected, path = _select_failure(error, validator)
    content: dict[str, JsonValue] = {
        "code": "invalid_arguments",
        "feedback_version": "schema-argument-feedback/v2",
        "constraint": str(selected.validator),
        "schema_path": _pointer(path)[:2048],
        "instruction": "Correct arguments against this tool schema before retrying.",
    }
    if origin := _physical_schema_path(schema, selected):
        content["source_schema_path"] = origin
    declared = _declared_instance_path(selected)
    instance_path = _pointer(selected.absolute_path) if selected.absolute_path else ""
    if not declared:
        content["instance_path_redacted"] = True
    elif len(instance_path) <= 2048:
        content["instance_path"] = instance_path
    else:
        content["instance_path_truncated"] = True
    if selected.validator == "required" and isinstance(selected.instance, dict):
        missing = [name for name in selected.validator_value if name not in selected.instance]
        content["missing_fields"] = [name for name in missing[:20] if len(name) <= 256]
        if len(missing) > 20 or any(len(name) > 256 for name in missing[:20]):
            content["missing_fields_truncated"] = True
    if (selected.validator == "additionalProperties" and selected.validator_value is False
            and isinstance(selected.instance, dict)):
        properties = selected.schema.get("properties", {})
        patterns = selected.schema.get("patternProperties", {})
        unexpected = [name for name in selected.instance if name not in properties
                      and not any(re.search(pattern, name) for pattern in patterns)]
        content["unexpected_fields"] = [name for name in unexpected[:20] if len(name) <= 256]
        if len(unexpected) > 20 or any(len(name) > 256 for name in unexpected[:20]):
            content["unexpected_fields_truncated"] = True
    union_types = _union_type_hint(selected) if selected.path else None
    if (selected.validator == "type" and selected.path) or union_types:
        last = next(reversed(selected.path))
        field = last if declared and isinstance(last, str) else _schema_field(selected)
        if field is not None:
            content["field"] = field[:80]
        content["received_type"] = type(selected.instance).__name__
        expected = selected.validator_value
        if union_types:
            content["expected_types"] = union_types[:8]
        elif isinstance(expected, str):
            content["expected_type"] = expected[:40]
        elif isinstance(expected, list):
            content["expected_types"] = expected[:8]
    return content
