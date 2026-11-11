"""Native/legacy interrupt normalization, shared by all LangGraph projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ksadk.interaction.schema import InputSchemaError, require_json, validate_schema

_SDK_INPUT_SCHEMA_KEY = "x-ksadk-request-input"


def _declares_approval(value: Mapping[str, Any]) -> bool:
    from ksadk.interaction.langgraph_approval import approval_contract

    # An arbitrary prompt/type/tool name does not declare approve/reject
    # semantics. Preserve explicit SDK approvals and the native HITL contract.
    return (
        value.get("kind") in ("approval", "legacy_review")
        or value.get("type") in ("approval", "legacy_review")
        or approval_contract(value) is not None
    )


def _sdk_input_schema_descriptor(schema: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {_SDK_INPUT_SCHEMA_KEY: {"version": 1, "sha256": sha256(encoded.encode()).hexdigest()}}


def _native_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # Pydantic cannot register $refs embedded through WithJsonSchema. Keep the
    # full schema in interrupt.value, with a versioned descriptor binding it.
    stack: list[Any] = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if isinstance(value.get("$ref"), str):
                return _sdk_input_schema_descriptor(schema)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return schema


@dataclass(frozen=True)
class NormalizedInterrupt:
    native_id: str
    value: Any
    kind: str
    prompt: str | None
    schema: dict[str, Any] | None
    schema_source: str
    schema_conflict: bool = False
    presentation: dict[str, Any] | None = None


def normalize_interrupt(interrupt: Any) -> NormalizedInterrupt:
    if isinstance(interrupt, Mapping):
        native_id = str(interrupt.get("id") or interrupt.get("approval_request_id") or "")
        value = interrupt.get("value")
        native_schema = interrupt.get("response_schema")
    else:
        native_id = str(getattr(interrupt, "id", "") or "")
        value = getattr(interrupt, "value", None)
        native_schema = getattr(interrupt, "response_schema", None)
    legacy = value if isinstance(value, Mapping) else {}
    if legacy.get("kind") == "external_authorization":
        from ksadk.mcp_runtime.oauth.langgraph import MARKER, SCHEMA
        if legacy.get("schema") != SCHEMA or not isinstance(legacy.get("wait_id"), str):
            raise InputSchemaError("Unknown MCP authorization contract")
        # Private adapter carrier only. The Kernel intercepts this marker
        # before publishing or persisting any ordinary Interaction/v1 form.
        return NormalizedInterrupt(native_id=native_id, value=value, kind="structured_input",
            prompt="需要授权 MCP 服务", schema={"type": "object", MARKER: legacy["wait_id"]},
            schema_source=SCHEMA)
    structured = legacy.get("kind") == "structured_input"
    legacy_schema = legacy.get("schema", {}) if structured else None
    sdk_descriptor = isinstance(native_schema, dict) and _SDK_INPUT_SCHEMA_KEY in native_schema
    if sdk_descriptor:
        if not structured or not isinstance(legacy_schema, dict):
            raise InputSchemaError("SDK input schema descriptor requires its saved schema")
        schema = validate_schema(legacy_schema)
        if native_schema != _sdk_input_schema_descriptor(schema):
            raise InputSchemaError("SDK input schema descriptor does not match its saved schema")
    else:
        schema = native_schema if native_schema is not None else legacy_schema
    if schema is not None or structured:
        schema = validate_schema(schema)
    elif not _declares_approval(legacy):
        # Native interrupt(value) accepts any JSON answer. Missing schema is
        # not an approval declaration and must not invent decision fields.
        schema = {}
    prompt = legacy.get("prompt", legacy.get("message"))
    if not isinstance(prompt, str):
        prompt = value if isinstance(value, str) else None
    presentation = legacy.get("presentation")
    if isinstance(presentation, dict) and "a2ui" in presentation:
        from ksadk.a2ui.presentation import prepare_presentation

        presentation = prepare_presentation(presentation)
    conflict = (
        not sdk_descriptor
        and native_schema is not None
        and legacy_schema is not None
        and native_schema != legacy_schema
    )
    if structured and schema is not None and "initial_values" in legacy:
        # JSON Schema's standard default annotation transports an initial form
        # draft without expanding Interaction/v1 or changing validation rules.
        # It does not fill a missing answer or missing required fields.
        schema = {**schema, "default": deepcopy(require_json(legacy["initial_values"]))}
    return NormalizedInterrupt(
        native_id=native_id,
        value=value,
        kind="structured_input" if schema is not None else "approval",
        prompt=prompt,
        schema=schema,
        schema_source=("native" if native_schema is not None else "legacy" if structured
                       else "default" if schema is not None else "none"),
        schema_conflict=conflict,
        presentation=deepcopy(presentation) if isinstance(presentation, dict) else None,
    )


__all__ = ["NormalizedInterrupt", "normalize_interrupt"]
