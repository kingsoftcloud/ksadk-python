"""AgentEngine A2A resource ID validation."""

from __future__ import annotations

import re

_UUID4_HEX = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")


def is_a2a_resource_id(value: str, prefix: str) -> bool:
    """Accept persisted Server IDs and the original prefixed SDK declarations.

    Preserve the exact ID: adding/removing prefixes changes registry lookup,
    permit claims and target binding. Server spaces use as-<12 hex>, registered
    agents and versions use compact UUID4, while tasks retain a2a-task-.
    """
    if value.startswith(prefix) and _UUID4_HEX.fullmatch(value.removeprefix(prefix)):
        return True
    if prefix in {"a2a-agent-", "a2a-version-"}:
        return bool(_UUID4_HEX.fullmatch(value))
    if prefix == "a2a-space-":
        return bool(re.fullmatch(r"as-[0-9a-f]{12}", value))
    return False


def require_a2a_resource_id(value: str, prefix: str, *, field_name: str) -> str:
    """Validate an AgentEngine A2A resource ID and return it unchanged."""
    if not is_a2a_resource_id(value, prefix):
        raise ValueError(f"{field_name} must use a Server resource ID or {prefix}<uuid4_hex>")
    return value


__all__ = ["is_a2a_resource_id", "require_a2a_resource_id"]
