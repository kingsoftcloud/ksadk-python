"""Bounded extraction from declared tool/MCP carriers, never from chat text."""

import json
from collections.abc import Mapping

A2UI_MIME = "application/a2ui+json"


def extract_operations(value, *, depth=0):
    if depth > 8:
        raise ValueError("A2UI carrier nesting exceeds budget")
    if hasattr(value, "content"):
        value = value.content
    if isinstance(value, str):
        if len(value.encode()) > 1_048_576:
            return []
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, list):
        if len(value) > 4096:
            raise ValueError("A2UI carrier exceeds budget")
        return [op for item in value for op in extract_operations(item, depth=depth + 1)]
    if not isinstance(value, Mapping):
        return []
    if "a2ui_operations" in value:
        operations = value["a2ui_operations"]
        if not isinstance(operations, list) or not operations:
            raise ValueError("Invalid declared A2UI operation list")
        return operations
    if value.get("mimeType", value.get("mime_type")) == A2UI_MIME:
        payload = value.get("data", value.get("json"))
        if isinstance(payload, str):
            payload = json.loads(payload)
        operations = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(operations, list):
            raise ValueError("Invalid A2UI MIME payload")
        return operations
    if value.get("structuredContent") is not None:
        return extract_operations(value["structuredContent"], depth=depth + 1)
    if value.get("type") == "text":
        return extract_operations(value.get("text"), depth=depth + 1)
    if "content" in value:
        return extract_operations(value["content"], depth=depth + 1)
    return []
