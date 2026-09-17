"""Public projections and scoped member identities for Teams decisions."""

from __future__ import annotations

from typing import Any


def public(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: public(item) for key, item in value.items() if not key.startswith("_")}
        if "_policy" in value:
            result["policy"] = public(value["_policy"])
        return result
    if isinstance(value, list):
        return [public(item) for item in value]
    return value


def member_key(group_id: str, member_id: str) -> str:
    return f"{group_id}:{member_id}"


def run_member_key(run_id: str, member_id: str) -> str:
    return f"{run_id}:{member_id}"
