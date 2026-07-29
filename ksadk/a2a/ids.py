"""AgentEngine A2A resource ID validation."""

from __future__ import annotations

import re

_UUID4_HEX = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")


def is_a2a_resource_id(value: str, prefix: str) -> bool:
    """Return whether value is `<prefix><uuid4_hex>` with a lowercase compact UUID."""
    return value.startswith(prefix) and bool(_UUID4_HEX.fullmatch(value.removeprefix(prefix)))


def require_a2a_resource_id(value: str, prefix: str, *, field_name: str) -> str:
    """Validate an AgentEngine A2A resource ID and return it unchanged."""
    if not is_a2a_resource_id(value, prefix):
        raise ValueError(f"{field_name} must use the {prefix}<uuid4_hex> format")
    return value


__all__ = ["is_a2a_resource_id", "require_a2a_resource_id"]
