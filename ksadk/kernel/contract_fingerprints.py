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
    "d4a66a7249e10375d32d6a83434fde1d16ee6721e3a09ea03ed71217ee742d62"
)


def runtime_capability_matrix_digest(matrix: Any) -> str:
    """Return the stable SHA-256 of the RuntimeCapabilityMatrix wire value.

    ``model_dump(mode=\"json\")`` is deliberate: it binds the digest to the
    public typed matrix rather than a framework object's in-memory layout.
    JSON key sort and compact separators make the value independent of Python
    dict insertion order and whitespace.
    """

    dump = matrix.model_dump(mode="json")
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
]
