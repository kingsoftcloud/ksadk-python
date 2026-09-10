"""Harness-owned validation for immutable platform resource references.

The Harness consumes version-pinned references but must not depend on the
Studio domain package that authors them.  Studio and other control planes may
serialize their own domain objects into these canonical strings.
"""

from __future__ import annotations

import re

SUPPORTED_REF_KINDS: frozenset[str] = frozenset(
    {
        "agent-project",
        "agent-revision",
        "agent-template",
        "agent-template-version",
        "mcp",
        "mcp-binding",
        "skill",
        "model-profile",
        "knowledge",
        "memory-policy",
        "policy",
        "evalset",
        "evalpack",
        "verification",
        "artifact",
        "workspace",
        "run",
        "trace",
    }
)

_REF_PATTERN = re.compile(
    r"^(?P<kind>[a-z][a-z0-9-]*)://(?P<id>[^/@#\s]+)"
    r"(?:@(?P<version>[^/#\s]+))?"
    r"(?:#(?P<hash>[^\s]+))?$"
)


class ResourceRefError(ValueError):
    """Raised when a resource reference is malformed or not version-pinned."""


def parse_resource_ref(value: str) -> tuple[str, str, str | None, str | None]:
    match = _REF_PATTERN.match(value.strip())
    if match is None:
        raise ResourceRefError(f"资源引用格式无效: {value!r}")
    kind = match.group("kind")
    if kind not in SUPPORTED_REF_KINDS:
        raise ResourceRefError(f"资源引用类型不受支持: {kind}")
    version = match.group("version")
    if version is None or version == "latest":
        raise ResourceRefError(f"资源引用必须固定版本，不允许 latest: {value!r}")
    return kind, match.group("id"), version, match.group("hash")


def validate_resource_ref(value: str) -> str:
    parse_resource_ref(value)
    return value


__all__ = [
    "SUPPORTED_REF_KINDS",
    "ResourceRefError",
    "parse_resource_ref",
    "validate_resource_ref",
]
