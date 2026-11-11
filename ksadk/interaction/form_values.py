"""Conservative transport adaptation for answers to saved input Schemas.

This is deliberately separate from strict JSON Schema validation. It is used
only at form submission boundaries, never for arbitrary tool or model output.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote

from ksadk.interaction.schema import _validator, require_json, validate_answer, validate_schema

_NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?\Z", re.ASCII)
_MAX_SAFE_INTEGER = 2**53 - 1


def _scalar(value: Any, types: list[str]) -> Any:
    if not isinstance(value, str):
        return value
    if "boolean" in types and value in {"true", "false"}:
        return value == "true"
    if (not {"integer", "number"}.intersection(types)
            or len(value) > 1024 or not _NUMBER.fullmatch(value)):
        return value
    try:
        exact = Decimal(value)
        number = float(value)
        # Compare decimal round trips, not binary float equality. Reject
        # overflow, underflow and loss of significant decimal digits.
        round_trip = Decimal(str(number))
        if not exact.is_finite() or not round_trip.is_finite() or round_trip != exact:
            return value
        if "number" not in types:
            if exact != exact.to_integral_value() or abs(exact) > _MAX_SAFE_INTEGER:
                return value
            return int(exact)
        return number
    except (InvalidOperation, ValueError, OverflowError):
        return value


def normalize_form_answer(schema: dict[str, Any], answer: Any) -> Any:
    """Adapt unambiguous scalar strings, then strictly validate the whole answer.

    Existing valid values win, including strings in union Schemas. No defaults,
    field removal, empty/null substitution, JSON parsing or fractional rounding.
    Ambiguous alternatives and unsupported complex mappings remain validation
    errors. The caller's Schema and answer are never modified.
    """
    require_json(answer)
    checked = validate_schema(schema)
    validator = _validator(json.dumps(checked, sort_keys=True, allow_nan=False))
    nodes = 0

    def valid(node, value):
        try:
            return validator.evolve(schema=node).is_valid(value)
        except Exception:
            return False  # The final strict validator supplies sanitized diagnostics.

    def reference(ref):
        if not isinstance(ref, str) or not ref.startswith("#"):
            return None
        fragment = unquote(ref[1:])
        if not fragment:
            return checked
        if not fragment.startswith("/"):
            return None
        target: Any = checked
        try:
            for part in fragment[1:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
            return target
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def visit(node, value, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 8192 or depth > 48 or not isinstance(node, dict) or valid(node, value):
            return value
        original = value
        target = reference(node.get("$ref"))
        if target is not None:
            value = visit(target, value, depth + 1)
        # Combining constraints can describe different fields of the same object.
        for branch in node.get("allOf", []):
            value = visit(branch, value, depth + 1)
        alternatives = node.get("oneOf", node.get("anyOf"))
        if isinstance(alternatives, list):
            candidates = []
            for branch in alternatives:
                candidate = visit(branch, value, depth + 1)
                if valid(node, candidate) and not any(
                    candidate == other for other in candidates
                ):
                    candidates.append(candidate)
            return candidates[0] if len(candidates) == 1 else original
        types = node.get("type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(value, dict):
            result = dict(value)
            properties = node.get("properties", {})
            patterns = node.get("patternProperties", {})
            for key, child in value.items():
                schemas = [properties[key]] if key in properties else []
                schemas.extend(sub for pattern, sub in patterns.items() if re.search(pattern, key))
                if not schemas and isinstance(node.get("additionalProperties"), dict):
                    schemas = [node["additionalProperties"]]
                for sub in schemas:
                    child = visit(sub, child, depth + 1)
                result[key] = child
            value = result
        elif isinstance(value, list):
            prefix = node.get("prefixItems", [])
            items = node.get("items", {})
            if isinstance(items, list):
                prefix, items = items, node.get("additionalItems", {})
            value = [visit(prefix[i] if i < len(prefix) else items, child, depth + 1)
                     for i, child in enumerate(value)]
        else:
            value = _scalar(value, types)
        return value

    return validate_answer(checked, visit(checked, answer))
