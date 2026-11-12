"""Link offline Schema assets for core's schema-only catalog compiler.

The original document is still the validation authority and digest input.
Only Schema positions are visited: example/default business data is untouched.
"""

import json
from copy import deepcopy
from urllib.parse import urljoin, urlsplit

from ksadk.a2ui.budget import MAX_CATALOG_BYTES, MAX_CATALOG_DEPTH, MAX_CATALOG_NODES

_MAPS = {"components", "functions", "$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
_SINGLE = {"theme", "items", "additionalProperties", "additionalItems", "contains", "propertyNames",
           "not", "if", "then", "else", "unevaluatedProperties", "unevaluatedItems", "contentSchema"}
_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_COMMON = "https://a2ui.org/specification/v0_9/common_types.json"


def link_catalog(document, registry):
    root = document.get("$id") or document["catalogId"]
    resolving = set()
    visits = 0
    copied_nodes = 0
    copied_bytes = 0

    def copy_value(value):
        # Defaults/examples are opaque business data, but each duplicated value
        # still consumes the output budget before deepcopy allocates it.
        nonlocal copied_nodes, copied_bytes
        stack = [value]
        while stack:
            item = stack.pop()
            copied_nodes += 1
            if copied_nodes > MAX_CATALOG_NODES:
                raise ValueError("A2UI catalog reference expansion exceeds its node budget")
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            copied_bytes += len(chunk.encode())
            if copied_bytes > MAX_CATALOG_BYTES:
                raise ValueError("A2UI catalog reference expansion exceeds its byte budget")
        return deepcopy(value)

    def address(base, ref):
        if ref.startswith("#"):
            return base.partition("#")[0] + ref
        return ref if urlsplit(ref).scheme else urljoin(base, ref)

    def link(schema, base, *, resource=False, depth=0):
        nonlocal visits
        visits += 1
        if visits > MAX_CATALOG_NODES or depth > MAX_CATALOG_DEPTH:
            raise ValueError("A2UI catalog reference expansion exceeds its budget")
        if not isinstance(schema, dict):
            return copy_value(schema)
        scope = (address(base, schema["$id"]) if isinstance(schema.get("$id"), str)
                 and schema["$id"] != base else base)
        references = {}
        for key in ("$ref", "$dynamicRef"):
            ref = schema.get(key)
            if not isinstance(ref, str):
                continue
            target = address(scope, ref)
            try:
                resolved = registry.resolver(scope).lookup(ref)
            except Exception as exc:
                raise ValueError("A2UI catalog has an unresolved offline reference") from exc
            if target.partition("#")[0] == root:
                references[key] = "#" + target.partition("#")[2]
            elif target.partition("#")[0] != _COMMON:
                if target in resolving:
                    raise ValueError("Recursive external A2UI Schema assets cannot be linked")
                resolving.add(target)
                try:
                    replacement = link(
                        resolved.contents, target.partition("#")[0],
                        resource=True, depth=depth + 1,
                    )
                finally:
                    resolving.remove(target)
                siblings = {name: value for name, value in schema.items() if name != key}
                if not siblings:
                    return replacement
                result = copy_value({})
                result[copy_value("allOf")] = copy_value([]) + [
                    replacement, link(siblings, scope, resource=resource, depth=depth + 1),
                ]
                return result
        result = copy_value({})
        for key, value in schema.items():
            key = copy_value(key)
            if key in _MAPS and isinstance(value, dict):
                result[key] = copy_value({})
                for name, child in value.items():
                    result[key][copy_value(name)] = link(
                        child, scope, resource=resource, depth=depth + 1,
                    )
            elif key in _SINGLE and isinstance(value, (dict, bool)):
                result[key] = link(value, scope, resource=resource, depth=depth + 1)
            elif key in _ARRAYS and isinstance(value, list):
                result[key] = copy_value([]) + [
                    link(child, scope, resource=resource, depth=depth + 1) for child in value
                ]
            else:
                result[key] = copy_value(references.get(key, value))
        if resource:
            result.pop("$id", None)
            result.pop("$schema", None)
            result.pop("$anchor", None)
        return result

    return link(document, root)
