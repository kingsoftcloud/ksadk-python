"""Lossless provider projection for closed object unions.

Some tool consumers enumerate only top-level properties. Expose those fields
without removing the original oneOf, coercing values, or changing the pinned
host definition. Schemas whose implication cannot be proved here stay intact.
"""

from __future__ import annotations

import json
from copy import deepcopy

from pydantic import JsonValue

_ROOT_KEYS = {"type", "oneOf", "title", "description"}
_BRANCH_KEYS = {
    "type", "properties", "required", "additionalProperties", "title", "description",
    # These conjunctive restrictions cannot admit a property excluded by the
    # same closed branch. Retain them unchanged in the original oneOf.
    "oneOf", "dependentRequired",
}
_CONTEXT_KEYS = {
    "$ref", "$dynamicRef", "$recursiveRef", "$id", "id", "$anchor",
    "$dynamicAnchor", "$recursiveAnchor", "$schema",
}


def _context_dependent(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return bool(_CONTEXT_KEYS.intersection(value)) or any(
            _context_dependent(child) for child in value.values()
        )
    return isinstance(value, list) and any(_context_dependent(child) for child in value)


def _literal_type(values: list[JsonValue]) -> str | None:
    """Return a JSON type implied by every literal, keeping bool distinct."""
    kinds = set()
    for value in values:
        if value is None:
            kinds.add("null")
        elif isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, int):
            kinds.add("integer")
        elif isinstance(value, float):
            kinds.add("number")
        elif isinstance(value, str):
            kinds.add("string")
        elif isinstance(value, list):
            kinds.add("array")
        elif isinstance(value, dict):
            kinds.add("object")
        else:
            return None
    if kinds and kinds <= {"integer", "number"}:
        return "number" if "number" in kinds else "integer"
    return next(iter(kinds)) if len(kinds) == 1 else None


def _explicit_literal_type(schema: JsonValue) -> JsonValue:
    """Make a redundant literal type visible to strict provider compilers."""
    if not isinstance(schema, dict) or "type" in schema:
        return schema
    values = [schema["const"]] if "const" in schema else schema.get("enum")
    if not isinstance(values, list) or not values:
        return schema
    inferred = _literal_type(values)
    return {**schema, "type": inferred} if inferred is not None else schema


def model_tool_parameters(parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Add only constraints implied by every existing closed object branch."""
    projected = deepcopy(parameters)
    branches = projected.get("oneOf")
    if (
        projected.get("type") != "object"
        or not set(projected).issubset(_ROOT_KEYS)
        or not isinstance(branches, list)
        or not 2 <= len(branches) <= 32
        or _context_dependent(projected)
    ):
        return projected
    properties: dict[str, list[JsonValue]] = {}
    property_identities: dict[str, set[str]] = {}
    common_required: set[str] | None = None
    for branch in branches:
        if (
            not isinstance(branch, dict)
            or not set(branch).issubset(_BRANCH_KEYS)
            or branch.get("type") != "object"
            or branch.get("additionalProperties") is not False
            or not isinstance(branch.get("properties"), dict)
            or not isinstance(branch.get("required", []), list)
            or any(not isinstance(name, str) for name in branch.get("required", []))
        ):
            return projected
        required = set(branch.get("required", []))
        common_required = required if common_required is None else common_required & required
        for name, schema in branch["properties"].items():
            choices = properties.setdefault(name, [])
            identities = property_identities.setdefault(name, set())
            identity = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if identity not in identities:
                choices.append(schema)
                identities.add(identity)
    if len(properties) > 128:
        return projected
    fields = {}
    for name, choices in properties.items():
        if len(choices) == 1:
            fields[name] = choices[0]
        elif all(isinstance(choice, dict) and set(choice) == {"const"} for choice in choices):
            fields[name] = {"enum": [choice["const"] for choice in choices]}
        else:
            fields[name] = {"anyOf": choices}
    # A valid branch forbids unknown fields; each present field therefore
    # satisfies at least one of these alternatives. Keeping oneOf enforces
    # the original per-action requirements, exclusions and type combinations.
    projected["properties"] = deepcopy({
        name: _explicit_literal_type(schema) for name, schema in fields.items()
    })
    projected["additionalProperties"] = False
    if common_required:
        projected["required"] = sorted(common_required)
    return projected
