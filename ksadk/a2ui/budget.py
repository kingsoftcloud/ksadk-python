"""Bound catalog input before copying, validating, or expanding offline refs."""

import json

MAX_CATALOG_BYTES = 1_048_576
MAX_CATALOG_DEPTH = 64
MAX_CATALOG_NODES = 32_768
MAX_CATALOG_COMPONENTS = 2048
MAX_CATALOG_RESOURCES = 128


def bounded_catalog_input(value):
    if isinstance(value, str):
        if len(value) > MAX_CATALOG_BYTES or len(value.encode()) > MAX_CATALOG_BYTES:
            raise ValueError("A2UI catalog exceeds its byte budget")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("Invalid A2UI catalog JSON") from exc
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > MAX_CATALOG_DEPTH or nodes > MAX_CATALOG_NODES:
            raise ValueError("A2UI catalog exceeds its depth/node budget")
        if isinstance(item, dict):
            if len(item) > MAX_CATALOG_NODES:
                raise ValueError("A2UI catalog exceeds its node budget")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > MAX_CATALOG_NODES:
                raise ValueError("A2UI catalog exceeds its node budget")
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str) and len(item) > MAX_CATALOG_BYTES:
            raise ValueError("A2UI catalog exceeds its byte budget")
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            size += len(chunk.encode())
            if size > MAX_CATALOG_BYTES:
                break
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid A2UI catalog JSON") from exc
    if size > MAX_CATALOG_BYTES:
        raise ValueError("A2UI catalog exceeds its byte budget")
    if isinstance(value, dict):
        components = value.get("components")
        if isinstance(components, dict) and len(components) > MAX_CATALOG_COMPONENTS:
            raise ValueError("A2UI catalog exceeds its component budget")
        resources = value.get("ksadkResources")
        if isinstance(resources, list) and len(resources) > MAX_CATALOG_RESOURCES:
            raise ValueError("A2UI catalog exceeds its offline resource budget")
    return value
