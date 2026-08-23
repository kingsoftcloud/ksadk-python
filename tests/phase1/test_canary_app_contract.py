# -*- coding: utf-8 -*-
"""Regression locks for the deployable Phase 1 canary harness."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from ksadk.kernel.contract_fingerprints import AGENT_KERNEL_V1_AGGREGATE_DIGEST
from tests.phase1 import canary_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_canary_reports_the_packaged_frozen_contract() -> None:
    assert canary_app.CONTRACT_DIGEST == AGENT_KERNEL_V1_AGGREGATE_DIGEST


def test_canary_uses_pod_uid_as_fencing_owner(monkeypatch) -> None:
    monkeypatch.setenv("POD_UID", "pod-uid-a")
    assert canary_app.activation_id() == "pod-uid-a"


def test_canary_lease_ttl_comes_from_projection(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_KERNEL_LEASE_TTL_SECONDS", "17")
    assert canary_app.lease_ttl_seconds() == 17.0


def test_canary_store_namespace_is_explicitly_isolated(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_KERNEL_STORE_NAMESPACE", "phase1-run-42")
    assert canary_app.store_namespace() == "phase1-run-42"


def test_canary_drill_hooks_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("PHASE1_CANARY_TEST_HOOKS", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        canary_app._require_test_hooks()
    assert exc_info.value.status_code == 404


def test_canary_exposes_required_managed_pg_drill_hooks() -> None:
    paths = {route.path for route in canary_app.app.routes}
    assert {
        "/test/worker",
        "/test/snapshot",
        "/test/drills/stale-fence",
        "/test/cleanup",
    } <= paths


def test_canary_requires_an_external_managed_postgres() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    deployment = (
        REPO_ROOT / "docs/superpowers/evidence/phase1/canary-hosted/deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "PHASE1_CANARY_POSTGRES_DSN must reference an external managed PostgreSQL" in makefile
    assert "PHASE1_CANARY_KUBECONFIG ?= $(HOME)/.kube/config-2fc1210d" in makefile
    assert "agent-kernel-store" in deployment
    assert not (REPO_ROOT / "docs/superpowers/evidence/phase1/canary/postgres.yaml").exists()
