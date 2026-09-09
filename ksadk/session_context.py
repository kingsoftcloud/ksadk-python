"""Immutable per-invocation session labels, separate from caller metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class SessionContext:
    schema_version: int = 1
    revision: int = 0
    tags: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported session context schema_version")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Session context revision must be a nonnegative integer")
        if not isinstance(self.tags, Mapping) or len(self.tags) > 32:
            raise ValueError("Session context supports at most 32 tags")
        for key, value in self.tags.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
                or key.startswith("agentengine.")
                or not isinstance(value, str)
                or len(value) > 256
            ):
                raise ValueError("Invalid session tag")
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "SessionContext":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ValueError("Session context must be an object")
        return cls(
            schema_version=payload.get("schema_version", 1),
            revision=payload.get("revision", 0),
            tags=payload.get("tags", {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "tags": dict(self.tags),
        }


def split_session_context(
    metadata: Mapping[str, Any] | None,
) -> tuple[SessionContext, dict[str, Any]]:
    """Extract the platform envelope before any event or prompt projection."""
    cleaned = dict(metadata or {})
    controls = cleaned.get("agentengine")
    snapshot = None
    if isinstance(controls, Mapping):
        controls = dict(controls)
        snapshot = controls.pop("session_context", None)
        cleaned["agentengine"] = controls
    return SessionContext.from_payload(snapshot), cleaned
