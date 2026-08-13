"""Small control-plane payload helpers shared by Agent API operations."""

from __future__ import annotations

from typing import Any

from ksadk.configs.env_registry import is_sensitive_env_var


def environment_variables_payload(value: Any) -> list[dict[str, Any]]:
    """Normalize environment variables while preserving their sensitivity flag."""

    if isinstance(value, dict):
        return [
            {
                "Key": str(key),
                "Value": str(item),
                "IsSensitive": is_sensitive_env_var(str(key)),
            }
            for key, item in value.items()
        ]
    if isinstance(value, list):
        return value
    return []


__all__ = ["environment_variables_payload"]
