"""Deterministic identities for canonical runtime scopes, items, and events."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


def stable_scope_id(framework: str, *native_components: Any) -> str:
    """Derive a stable execution-scope id from source-native components."""

    return _stable_identity("scope", framework, native_components)


def stable_item_id(framework: str, *native_components: Any) -> str:
    """Derive a stable item id from source-native components."""

    return _stable_identity("item", framework, native_components)


def stable_part_id(framework: str, *native_components: Any) -> str:
    """Derive a stable content-part id from source-native components."""

    return _stable_identity("part", framework, native_components)


def stable_event_id(
    framework: str,
    scope_id: str,
    item_id: str,
    event_type: str,
    part_id: str,
    native_occurrence_id: str,
    chunk_ordinal: int,
) -> str:
    """Derive a mutation-occurrence id, distinct from the source item id."""

    return _stable_identity(
        "event",
        framework,
        (
            scope_id,
            item_id,
            event_type,
            part_id,
            native_occurrence_id,
            chunk_ordinal,
        ),
    )


def _stable_identity(kind: str, framework: Any, components: tuple[Any, ...]) -> str:
    if not components:
        raise ValueError("identity component must not be empty")
    normalized = [_normalize_component(framework)]
    normalized.extend(_normalize_component(component) for component in components)
    payload = json.dumps(
        {"components": normalized, "kind": kind},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"{kind}_{digest}"


def _normalize_component(component: Any) -> str:
    if component is None:
        raise ValueError("identity component must not be empty")
    if isinstance(component, bool):
        value = "true" if component else "false"
    elif isinstance(component, (str, int)):
        value = str(component)
    else:
        raise TypeError(
            f"identity component must be a string or integer, got {type(component).__name__}"
        )
    value = unicodedata.normalize("NFC", value)
    if not value.strip():
        raise ValueError("identity component must not be empty")
    return value


__all__ = ["stable_event_id", "stable_item_id", "stable_part_id", "stable_scope_id"]
