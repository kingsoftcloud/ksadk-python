# -*- coding: utf-8 -*-
"""Package-resident fingerprints for the frozen Agent Kernel wire contract.

The contract manifest lives at repository root for schema review, so it is not
available from an installed wheel.  Hosted Runtime therefore cannot trust an
environment value that merely *claims* compatibility: the supported aggregate
digest is shipped in this Python module.  The contract regression test locks
this constant to ``contracts/agent-kernel/v1/manifest.json``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

AGENT_KERNEL_V1_CONTRACT_SET = "agent-kernel/v1"
AGENT_KERNEL_V1_AGGREGATE_DIGEST = (
    "b610a25aae957306b9f84a2cc2c948b30d1ce6218585cfd2a91f96736b92d102"
)


def runtime_capability_matrix_wire_value(matrix: Any) -> dict[str, Any]:
    """Serialize the additive matrix without materializing absent v2 modes.

    Pydantic includes optional ``None`` defaults in ``model_dump``.  Omitting
    those additive top-level keys preserves the exact pre-extension wire value
    and capability digest for runtimes that do not publish execution controls
    or an interaction delivery mode.
    """

    dump = matrix.model_dump(mode="json")
    for key in ("interaction_mode", "goal", "loop", "plan", "execution_policy", "scoped_cancel"):
        if dump.get(key) is None:
            dump.pop(key, None)
    return dump


def runtime_capability_matrix_digest(matrix: Any) -> str:
    """Return the stable SHA-256 of the RuntimeCapabilityMatrix wire value.

    ``model_dump(mode=\"json\")`` is deliberate: it binds the digest to the
    public typed matrix rather than a framework object's in-memory layout.
    JSON key sort and compact separators make the value independent of Python
    dict insertion order and whitespace.
    """

    dump = runtime_capability_matrix_wire_value(matrix)
    canonical = json.dumps(
        dump,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _required_capability_policy_digest(name: str, required: tuple[str, ...]) -> str:
    """Fingerprint a versioned minimum contract, not one adapter's full matrix."""

    payload = {"schema": "agent-kernel/required-capabilities/v1", "name": name,
               "required": list(required)}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


BASIC_A2A_CAPABILITY_DIGEST = _required_capability_policy_digest(
    "basic-a2a", ("cancel", "submit_interaction")
)
HARNESS_CANCEL_CAPABILITY_DIGEST = _required_capability_policy_digest(
    "harness-cancel", ("cancel",)
)


def runtime_capability_admission_digest(matrix: Any, expected: str) -> str:
    """Verify a selected minimum policy or retain the legacy exact-matrix pin.

    The complete matrix is still used for command admission and reported in
    health. A missing required capability returns the actual full digest so
    the existing fail-closed mismatch/readiness paths remain effective.
    """

    required = {
        BASIC_A2A_CAPABILITY_DIGEST: ("cancel", "submit_interaction"),
        HARNESS_CANCEL_CAPABILITY_DIGEST: ("cancel",),
    }.get(expected)
    if required is None:
        return runtime_capability_matrix_digest(matrix)
    if all(
        getattr(getattr(matrix, name, None), "supported", False) is True
        and getattr(getattr(matrix, name, None), "mode", None) in {"native", "emulated"}
        for name in required
    ):
        return expected
    return runtime_capability_matrix_digest(matrix)


__all__ = [
    "AGENT_KERNEL_V1_AGGREGATE_DIGEST",
    "AGENT_KERNEL_V1_CONTRACT_SET",
    "runtime_capability_matrix_digest",
    "runtime_capability_admission_digest",
    "BASIC_A2A_CAPABILITY_DIGEST",
    "HARNESS_CANCEL_CAPABILITY_DIGEST",
    "runtime_capability_matrix_wire_value",
]
