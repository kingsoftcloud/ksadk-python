# -*- coding: utf-8 -*-
"""Agent Kernel Phase 1 预发 gate 测试（Task 13 Step 1）。

对 ``scripts/phase1_preprod_gate.py`` 的 gate 逻辑本身做单元验证：
用 fake evidence 驱动 report 对象，不需要 --preprod，也不需要真实预发环境。
"""

from __future__ import annotations

import json

import pytest

from scripts.phase1_preprod_gate import (
    REQUIRED_CHECKS,
    evaluate_evidence,
    main,
)


def complete_fake_evidence() -> dict:
    """全部 required check pass 的最小合法 evidence（只有 ref/digest）。"""

    checks = {}
    for name in sorted(REQUIRED_CHECKS):
        checks[name] = {
            "status": "pass",
            "refs": {
                "helm_revision": 3,
                "image_digest": "sha256:" + "0" * 64,
                "contract_digest": "digest-" + name,
            },
        }
    return {"checks": checks}


@pytest.fixture
def report():
    """验收矩阵里的 report fixture：完整 fake evidence 的 gate 结果。"""

    return evaluate_evidence(
        complete_fake_evidence(), environment="fake", scenario="closure"
    )


async def test_gate_requires_every_closed_loop_evidence(report):
    required = {
        "contract_digest", "fifo", "idempotency", "queue_full", "reconnect",
        "cold_recovery", "stale_fence", "audit", "cross_repo_versions", "rollback",
    }
    assert required <= report.passed_checks
    assert report.skipped_checks.isdisjoint(required)


def test_gate_passes_on_complete_evidence(report):
    assert report.ok
    assert report.status == "pass"
    assert report.failed_checks == frozenset()
    assert REQUIRED_CHECKS <= report.passed_checks


def test_gate_fails_when_required_check_skipped():
    evidence = complete_fake_evidence()
    evidence["checks"]["rollback"] = {"status": "skip", "reason": "not run"}
    report = evaluate_evidence(evidence, environment="pre", scenario="closure")
    assert not report.ok
    assert "rollback" in report.skipped_checks
    assert "rollback" in report.failed_checks
    assert report.skipped_checks & set(REQUIRED_CHECKS)


def test_gate_fails_when_required_check_missing():
    evidence = complete_fake_evidence()
    del evidence["checks"]["stale_fence"]
    report = evaluate_evidence(evidence, environment="pre", scenario="closure")
    assert not report.ok
    assert "stale_fence" in report.failed_checks
    assert "stale_fence" not in report.passed_checks


def test_gate_fails_on_failed_check():
    evidence = complete_fake_evidence()
    evidence["checks"]["fifo"] = {"status": "fail", "reason": "order violated"}
    report = evaluate_evidence(evidence, environment="pre", scenario="closure")
    assert not report.ok
    assert "fifo" in report.failed_checks
    assert "fifo" not in report.passed_checks


@pytest.mark.parametrize(
    "forbidden",
    [
        {"dsn": "postgresql://user:pw@host/db"},
        {"secret_ref": "agent-kernel-postgres"},
        {"authorization": "Bearer abcdefghijklmnop"},
        {"api_key": "sk-1234567890"},
    ],
)
def test_gate_rejects_secret_shaped_evidence(forbidden):
    evidence = complete_fake_evidence()
    evidence["checks"]["audit"]["refs"] = forbidden
    report = evaluate_evidence(evidence, environment="pre", scenario="closure")
    assert not report.ok
    assert any("forbidden" in reason for reason in report.reasons)


def test_report_serialization_has_no_secrets(report):
    text = report.to_json()
    lowered = text.lower()
    for banned in ("postgresql://", "bearer ", "dsn", "secret", "password"):
        assert banned not in lowered


def test_gate_report_is_machine_readable(report):
    payload = json.loads(report.to_json())
    assert payload["status"] == "pass"
    assert set(payload["passed_checks"]) == set(REQUIRED_CHECKS)


def test_gate_cli_writes_report_and_exits_zero(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(complete_fake_evidence()))
    output = tmp_path / "report.json"
    code = main(
        [
            "--environment", "pre",
            "--scenario", "closure",
            "--evidence", str(evidence_path),
            "--output", str(output),
        ]
    )
    assert code == 0
    payload = json.loads(output.read_text())
    assert payload["status"] == "pass"
    assert payload["environment"] == "pre"
    assert payload["scenario"] == "closure"


def test_gate_cli_fails_and_exits_nonzero_on_missing_evidence(tmp_path):
    output = tmp_path / "report.json"
    code = main(
        [
            "--environment", "pre",
            "--scenario", "closure",
            "--evidence", str(tmp_path / "nope.json"),
            "--output", str(output),
        ]
    )
    assert code == 1
    payload = json.loads(output.read_text())
    assert payload["status"] == "fail"
    assert set(REQUIRED_CHECKS) <= set(payload["failed_checks"])
