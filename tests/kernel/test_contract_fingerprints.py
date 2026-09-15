# -*- coding: utf-8 -*-
"""Package fingerprints must remain locked to the reviewed v1 manifest."""
from __future__ import annotations

import json
from pathlib import Path

from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    AGENT_KERNEL_V1_CONTRACT_SET,
    runtime_capability_matrix_digest,
    runtime_capability_matrix_wire_value,
)
from ksadk.kernel.contracts import RuntimeCapability
from tests.kernel.control_harness import default_matrix


def test_packaged_agent_kernel_contract_digest_matches_frozen_manifest() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "contracts"
        / "agent-kernel"
        / "v1"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["contract_set"] == AGENT_KERNEL_V1_CONTRACT_SET
    assert manifest["aggregate_digest"] == AGENT_KERNEL_V1_AGGREGATE_DIGEST


def test_runtime_capability_digest_is_stable_for_the_wire_matrix() -> None:
    matrix = default_matrix()
    first = runtime_capability_matrix_digest(matrix)
    # Pydantic dumps nested fields in model order; rebuilding equivalent wire
    # content through a mapping must not change the ownership fingerprint.
    rebuilt = type(matrix).model_validate(matrix.model_dump(mode="json"))
    assert first == runtime_capability_matrix_digest(rebuilt)
    assert len(first) == 64


def test_execution_modes_are_bound_into_runtime_capability_digest() -> None:
    legacy = default_matrix()
    codex_modes = legacy.model_copy(
        update={
            "interaction_mode": "live_submit",
            "goal": RuntimeCapability(supported=True, mode="native"),
            "loop": RuntimeCapability(
                supported=False,
                mode="unavailable",
                reason="codex_loop_requires_run_control_spec",
            ),
            "plan": RuntimeCapability(supported=True, mode="native"),
        }
    )

    legacy_digest = runtime_capability_matrix_digest(legacy)
    assert legacy_digest == "b36b6ac71ef531c53d3f7d0a6b593ec6e9289d357bcc50b9ee2df6f4e1877a8d"
    assert runtime_capability_matrix_digest(codex_modes) != legacy_digest
    legacy_wire = runtime_capability_matrix_wire_value(legacy)
    assert legacy_wire["interaction_mode"] == "unavailable"
    assert {"goal", "loop", "plan"}.isdisjoint(legacy_wire)
    assert {"interaction_mode", "goal", "loop", "plan"} <= set(
        runtime_capability_matrix_wire_value(codex_modes)
    )
