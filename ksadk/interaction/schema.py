"""Offline JSON Schema validation shared by ingress and native input helpers."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError


class InputSchemaError(ValueError):
    code = "input_schema_invalid"


class InputValidationError(ValueError):
    code = "input_response_invalid"

    def __init__(self, issues: list[dict[str, str]]) -> None:
        self.issues = issues
        # Native validators and HTTP diagnostics must not echo submitted data.
        super().__init__("Response does not satisfy the input schema")


def require_json(value: Any) -> Any:
    """Accept JSON values without coercion, arbitrary encoders or non-finite numbers."""
    stack = [value]
    seen: set[int] = set()
    try:
        while stack:
            item = stack.pop()
            if item is None or isinstance(item, (str, bool, int)):
                continue
            if isinstance(item, float) and math.isfinite(item):
                continue
            if isinstance(item, (dict, list)):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                if isinstance(item, dict):
                    if not all(isinstance(key, str) for key in item):
                        raise TypeError("JSON keys must be strings")
                    stack.extend(item.values())
                else:
                    stack.extend(item)
                continue
            raise TypeError("Value is not JSON")
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InputValidationError(
            [{"path": "", "keyword": "json", "message": "A finite JSON value is required"}]
        ) from exc
    return value


_MAP_SCHEMAS = (
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
)
_SINGLE_SCHEMAS = (
    "additionalProperties",
    "additionalItems",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentSchema",
)


def _check_local_references(schema: dict) -> None:
    stack: list[Any] = [schema]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            reference = node.get(key)
            if isinstance(reference, str) and not reference.startswith("#"):
                raise InputSchemaError("Only local JSON Schema references are supported")
        for key in _MAP_SCHEMAS:
            if isinstance(node.get(key), dict):
                stack.extend(node[key].values())
        for key in _SINGLE_SCHEMAS:
            stack.append(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if isinstance(node.get(key), list):
                stack.extend(node[key])
        items = node.get("items")
        stack.extend(items if isinstance(items, list) else [items])
        dependencies = node.get("dependencies")
        if isinstance(dependencies, dict):
            stack.extend(value for value in dependencies.values() if isinstance(value, dict))


@lru_cache(maxsize=128)
def _validator(encoded: str):
    schema = json.loads(encoded)
    uri = schema.get("$schema")
    if uri is not None and not any(
        str(uri).rstrip("#") == str(known).rstrip("#") for known in validators.SPECIFICATIONS
    ):
        raise InputSchemaError("Unsupported JSON Schema draft")
    _check_local_references(schema)
    validator_type = validators.validator_for(schema, default=Draft202012Validator)
    try:
        validator_type.check_schema(schema)
    except SchemaError as exc:
        raise InputSchemaError("Invalid input JSON Schema") from exc
    return validator_type(schema, format_checker=FormatChecker())


def validate_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise InputSchemaError("The input schema must be a JSON Schema object")
    try:
        require_json(schema)
        encoded = json.dumps(schema, sort_keys=True, allow_nan=False)
    except InputValidationError as exc:
        raise InputSchemaError("The input schema must contain only JSON values") from exc
    _validator(encoded)
    return json.loads(encoded)


def validate_answer(schema: dict[str, Any], answer: Any) -> Any:
    require_json(answer)
    checked = validate_schema(schema)
    validator = _validator(json.dumps(checked, sort_keys=True, allow_nan=False))
    try:
        errors = list(islice(validator.iter_errors(answer), 10))
    except Exception as exc:
        raise InputSchemaError("Input schema references could not be resolved locally") from exc
    if errors:
        raise InputValidationError(
            [
                {
                    "path": "".join(
                        "/" + str(part).replace("~", "~0").replace("/", "~1")
                        for part in error.absolute_path
                    ),
                    "keyword": str(error.validator),
                    "message": "This field does not satisfy the requested constraint",
                }
                for error in errors
            ]
        )
    return answer


__all__ = [
    "InputSchemaError",
    "InputValidationError",
    "require_json",
    "validate_answer",
    "validate_schema",
]
