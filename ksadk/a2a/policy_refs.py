"""Executable built-in policy references; never resolve authored URLs."""
from typing import Literal

RUNTIME_POLICY_REF = "policy://a2a/runtime"
STRICT_POLICY_REF = "policy://a2a/strict"


def resolve_policy_ref(value: str | None) -> Literal["runtime", "strict"]:
    if value is None or value == RUNTIME_POLICY_REF:
        return "runtime"
    if value == STRICT_POLICY_REF:
        return "strict"
    raise ValueError("A2A_POLICY_REF_UNSUPPORTED")
