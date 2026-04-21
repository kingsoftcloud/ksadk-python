"""Memory backend manifest model validated against the bundled JSON Schema."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, Field

_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "memory_backend_manifest.schema.json"


class MemoryBackendManifest(BaseModel):
    """Memory backend manifest for OpenClaw runtime."""

    schema_version: Literal["v1"] = "v1"
    backend_type: Literal["openclaw_default", "mem0"]
    config: dict[str, Any] = Field(default_factory=dict)
    secrets_env: dict[str, str] = Field(default_factory=dict)


@lru_cache(maxsize=1)
def _schema_validator() -> Draft202012Validator:
    """Build and cache the canonical JSON Schema validator."""
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _format_schema_error(error: JsonSchemaValidationError) -> str:
    """Format schema validation errors with a stable dotted path."""
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"Invalid manifest at '{location}': {error.message}"


def _validate_against_schema(data: object) -> dict[str, Any]:
    """Validate a raw manifest payload against the bundled JSON Schema."""
    errors = sorted(
        _schema_validator().iter_errors(data),
        key=lambda error: error.json_path,
    )
    if errors:
        first_error = errors[0]
        raise ValueError(_format_schema_error(first_error)) from first_error
    if not isinstance(data, Mapping):
        raise ValueError("Invalid manifest at '<root>': manifest must be an object")
    return dict(data)


def parse_manifest(
    raw: MemoryBackendManifest | str | Mapping[str, Any] | None,
) -> MemoryBackendManifest | None:
    """Parse a memory backend manifest from model, JSON string, or mapping."""
    if raw is None:
        return None

    if isinstance(raw, MemoryBackendManifest):
        data: object = raw.model_dump(mode="python")
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
    else:
        data = dict(raw)

    validated = _validate_against_schema(data)
    return MemoryBackendManifest.model_validate(validated)
