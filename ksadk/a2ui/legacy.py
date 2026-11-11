"""Explicit compatibility for the SDK's original seven-component catalog.

Reused from the prior repair's legacy validator, never registered as official basic.
"""

from typing import Any

from jsonschema import Draft202012Validator

from ksadk.a2ui.models import BASIC_CATALOG, BASIC_CATALOG_ID, A2UIValidationError

FIELD_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "label": {"type": "string"},
        "placeholder": {"type": "string"},
        "type": {
            "enum": [
                "string",
                "str",
                "text",
                "int",
                "integer",
                "number",
                "float",
                "bool",
                "boolean",
            ]
        },
        "required": {"type": "boolean"},
        "value": {},
    },
    "additionalProperties": False,
}


def validate_legacy_component(component: dict[str, Any]) -> None:
    kind = component.get("component")
    if kind not in BASIC_CATALOG:
        raise A2UIValidationError(f"Unsupported legacy A2UI component: {kind!r}")
    unknown = set(component) - BASIC_CATALOG[kind] - {"id", "component", "children"}
    if unknown:
        raise A2UIValidationError(f"Unknown {kind} properties: {sorted(unknown)}")
    if not isinstance(component.get("id"), str) or not component["id"]:
        raise A2UIValidationError("A2UI component id is required")
    if kind == "Form":
        fields = component.get("fields", [])
        if not isinstance(fields, list) or len(fields) > 200:
            raise A2UIValidationError("Form.fields must be an array with at most 200 fields")
        names = set()
        for field in fields:
            errors = list(Draft202012Validator(FIELD_SCHEMA).iter_errors(field))
            if errors:
                raise A2UIValidationError("Invalid legacy Form field")
            name = field["name"]
            if name in names or name in {"__proto__", "constructor", "prototype"}:
                raise A2UIValidationError("Duplicate or unsafe Form field name")
            names.add(name)
    if "children" in component and (
        not isinstance(component["children"], list)
        or any(not isinstance(child, str) for child in component["children"])
    ):
        raise A2UIValidationError("A2UI children must be component ids")


def legacy_catalog_document():
    components = {}
    for name, props in BASIC_CATALOG.items():
        components[name] = {
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "component": {"const": name},
                **{prop: {} for prop in sorted(props)},
                "children": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "component"], "additionalProperties": False,
        }
    components["Form"]["properties"]["fields"] = {
        "type": "array", "maxItems": 200, "items": FIELD_SCHEMA,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": BASIC_CATALOG_ID, "catalogId": BASIC_CATALOG_ID,
        "components": components, "functions": {},
    }
