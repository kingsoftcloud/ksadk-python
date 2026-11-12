"""Schema-guided public display of submitted values, separate from native answers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote


class _DisplayBudgetExceeded(ValueError):
    pass


def submission_display_value(
    schema: dict[str, Any], value: Any, *, max_depth: int = 48, max_nodes: int = 8192
) -> Any:
    """Copy an answer, replacing declared secrets with ``None`` for public events.

    All alternatives contribute sensitive declarations, including alternatives
    which do not validate the answer. Only document-local references are used;
    unsupported reference scopes or exhausted budgets hide the complete answer.
    This does not change schemas, initial presentation data or private answers.
    """
    remaining = max_nodes

    def tick() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise _DisplayBudgetExceeded

    def reference(ref: Any) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise _DisplayBudgetExceeded
        fragment = unquote(ref[1:])
        if not fragment:
            return schema
        if fragment.startswith("/"):
            target: Any = schema
            for part in fragment[1:].split("/"):
                tick()
                key = part.replace("~1", "/").replace("~0", "~")
                try:
                    target = target[int(key)] if isinstance(target, list) else target[key]
                except (KeyError, IndexError, TypeError, ValueError):
                    raise _DisplayBudgetExceeded from None
            return target
        # Anchor/resource scopes require a resolver; never guess their target.
        raise _DisplayBudgetExceeded

    def expanded(candidates: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        pending = list(candidates)
        visited: set[int] = set()
        while pending:
            candidate = pending.pop()
            tick()
            if isinstance(candidate, bool):
                continue
            if not isinstance(candidate, dict):
                raise _DisplayBudgetExceeded
            if id(candidate) in visited:
                continue
            visited.add(id(candidate))
            if candidate is not schema and "$id" in candidate:
                raise _DisplayBudgetExceeded
            if "$dynamicRef" in candidate or "$recursiveRef" in candidate:
                raise _DisplayBudgetExceeded
            result.append(candidate)
            if "$ref" in candidate:
                pending.append(reference(candidate["$ref"]))
            for keyword in ("allOf", "anyOf", "oneOf"):
                pending.extend(candidate.get(keyword, []))
            # Conditional branches may describe sensitive values too.
            for keyword in ("if", "then", "else"):
                if keyword in candidate:
                    pending.append(candidate[keyword])
            for keyword in ("dependentSchemas", "dependencies"):
                pending.extend(child for child in candidate.get(keyword, {}).values()
                               if isinstance(child, (dict, bool)))
        return result

    def project(answer: Any, candidates: list[Any], depth: int) -> Any:
        tick()
        if depth > max_depth:
            raise _DisplayBudgetExceeded
        declarations = expanded(candidates)
        if any(
            entry.get("writeOnly") is True
            or entry.get("format") == "password"
            or entry.get("x-sensitive") is True
            or entry.get("x-codex-is-secret") is True
            for entry in declarations
        ):
            return None
        if isinstance(answer, dict):
            copied = {}
            for key, child in answer.items():
                child_schemas = []
                for entry in declarations:
                    properties = entry.get("properties", {})
                    matched = key in properties
                    if matched:
                        child_schemas.append(properties[key])
                    for pattern, child_schema in entry.get("patternProperties", {}).items():
                        if re.search(pattern, key):
                            matched = True
                            child_schemas.append(child_schema)
                    if not matched and "additionalProperties" in entry:
                        child_schemas.append(entry["additionalProperties"])
                    if "unevaluatedProperties" in entry:
                        child_schemas.append(entry["unevaluatedProperties"])
                copied[key] = project(child, child_schemas, depth + 1)
            return copied
        if isinstance(answer, list):
            copied_items = []
            for index, child in enumerate(answer):
                child_schemas = []
                for entry in declarations:
                    prefix = entry.get("prefixItems", [])
                    items = entry.get("items", {})
                    if index < len(prefix):
                        child_schemas.append(prefix[index])
                    elif isinstance(items, list):
                        child_schemas.append(items[index] if index < len(items)
                                             else entry.get("additionalItems", {}))
                    else:
                        child_schemas.append(items)
                    for keyword in ("contains", "unevaluatedItems"):
                        if keyword in entry:
                            child_schemas.append(entry[keyword])
                copied_items.append(project(child, child_schemas, depth + 1))
            return copied_items
        return answer

    try:
        return project(value, [schema], 0)
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError, re.error):
        # A partial traversal must never leave an uninspected secret in public.
        return None
