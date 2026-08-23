# -*- coding: utf-8 -*-
"""Regression locks for the deployable Phase 1 canary harness."""

from ksadk.kernel.contract_fingerprints import AGENT_KERNEL_V1_AGGREGATE_DIGEST
from tests.phase1 import canary_app


def test_canary_reports_the_packaged_frozen_contract() -> None:
    assert canary_app.CONTRACT_DIGEST == AGENT_KERNEL_V1_AGGREGATE_DIGEST


def test_canary_uses_pod_uid_as_fencing_owner(monkeypatch) -> None:
    monkeypatch.setenv("POD_UID", "pod-uid-a")
    assert canary_app.activation_id() == "pod-uid-a"


def test_canary_lease_ttl_comes_from_projection(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_KERNEL_LEASE_TTL_SECONDS", "17")
    assert canary_app.lease_ttl_seconds() == 17.0
