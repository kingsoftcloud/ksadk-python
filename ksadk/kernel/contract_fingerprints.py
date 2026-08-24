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
    "47e1003e03d97abeba232cc3e03a14b9cbcf78b1109870ccd2ce371f073b6211"
)


def runtime_capability_matrix_wire_value(matrix: Any) -> dict[str, Any]:
    """Serialize the additive matrix without materializing absent v2 modes.

    Pydantic includes optional ``None`` defaults in ``model_dump``.  Omitting
    those three top-level keys preserves the exact pre-extension wire value and
    capability digest for runtimes that do not publish goal/loop/plan.
    """

    dump = matrix.model_dump(mode="json")
    for key in ("goal", "loop", "plan"):
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


__all__ = [
    "AGENT_KERNEL_V1_AGGREGATE_DIGEST",
    "AGENT_KERNEL_V1_CONTRACT_SET",
    "runtime_capability_matrix_digest",
    "runtime_capability_matrix_wire_value",
]
