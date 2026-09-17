# -*- coding: utf-8 -*-
"""Agent Kernel Kernel 预发 gate 测试（Task 13 Step 1）。

对 ``scripts/kernel_preprod_gate.py`` 的 gate 逻辑本身做单元验证：
用 fake evidence 驱动 report 对象，不需要 --preprod，也不需要真实预发环境。
"""

from __future__ import annotations

import json

import pytest

from scripts.kernel_preprod_gate import (
    REQUIRED_CHECKS,
    TRACE_IDENTIFIERS_BY_CHECK,
    evaluate_evidence,
    main,
)

_HEX40 = "0123456789abcdef" * 2 + "abcdef0123456789"  # 40 hex chars
_HEX64 = "a" * 64


def _trace_detail(name: str) -> dict:
    """按 check 类型白名单给出可追溯标识的 detail。"""

    allowed = TRACE_IDENTIFIERS_BY_CHECK[name]
    if "digest" in allowed:
        return {"contract_digest": _HEX64, "source": "step.json#" + name}
    if "command_id" in allowed:
        return {"command_id": "f9ca0c95-f400-4fd3-ba2e-a7cc88d39f1d"}
    if "event_id" in allowed:
        return {"replay_event_count": 12, "seq_range": "1-12 unique"}
    return {"rollback_seconds": 11}


def complete_fake_evidence() -> dict:
    """全部 required check pass 的最小合法 evidence（status + 可追溯 detail）。"""

    checks = {}
    for name in sorted(REQUIRED_CHECKS - {"phase0_baseline"}):
        checks[name] = {
            "status": "pass",
            "detail": _trace_detail(name),
            "refs": {
                "helm_revision": 3,
                "image_digest": "sha256:" + _HEX64,
                "contract_digest": "digest-" + name,
            },
        }
    return {"checks": checks}


@pytest.fixture
def phase0_manifest(tmp_path):
    manifest = tmp_path / "phase0" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"schema_version": 1, "accepted": True, "commit": _HEX40}),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def contract_manifest(tmp_path):
    manifest = tmp_path / "contracts" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"aggregate_digest": _HEX64}), encoding="utf-8")
    return manifest


def evaluate(evidence: dict, *, phase0_manifest=None, contract_manifest=None):
    return evaluate_evidence(
        evidence,
        environment="fake",
        scenario="closure",
        phase0_manifest=phase0_manifest,
        contract_manifest=contract_manifest,
    )


@pytest.fixture
def report(phase0_manifest, contract_manifest):
    """验收矩阵里的 report fixture：完整 fake evidence 的 gate 结果。"""

    return evaluate(
        complete_fake_evidence(),
        phase0_manifest=phase0_manifest,
        contract_manifest=contract_manifest,
    )


async def test_gate_requires_every_closed_loop_evidence(report):
    required = {
        "contract_digest", "fifo", "idempotency", "queue_full", "reconnect",
        "cold_recovery", "stale_fence", "audit", "cross_repo_versions", "rollback",
        "phase0_baseline",
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
    report = evaluate(evidence)
    assert not report.ok
    assert "rollback" in report.skipped_checks
    assert "rollback" in report.failed_checks
    assert report.skipped_checks & set(REQUIRED_CHECKS)


def test_gate_fails_when_required_check_missing():
    evidence = complete_fake_evidence()
    del evidence["checks"]["stale_fence"]
    report = evaluate(evidence)
    assert not report.ok
    assert "stale_fence" in report.failed_checks
    assert "stale_fence" not in report.passed_checks


def test_gate_fails_on_failed_check():
    evidence = complete_fake_evidence()
    evidence["checks"]["fifo"] = {"status": "fail", "reason": "order violated"}
    report = evaluate(evidence)
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
    report = evaluate(evidence)
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


def test_gate_cli_writes_report_and_exits_zero(tmp_path, phase0_manifest, contract_manifest):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(complete_fake_evidence()))
    output = tmp_path / "report.json"
    code = main(
        [
            "--environment", "pre",
            "--scenario", "closure",
            "--evidence", str(evidence_path),
            "--phase0-manifest", str(phase0_manifest),
            "--contract-manifest", str(contract_manifest),
            "--output", str(output),
        ]
    )
    assert code == 0
    payload = json.loads(output.read_text())
    assert payload["status"] == "pass"
    assert payload["environment"] == "pre"
    assert payload["scenario"] == "closure"


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "docs/superpowers/evidence/phase0/manifest.json").is_file(),
    reason="internal phase0 manifest is not published in the public tree",
)
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
    # Phase 0 is a separately accepted manifest now; a missing scenario file
    # must still fail every behaviour check instead of regressing that fact.
    assert set(REQUIRED_CHECKS - {"phase0_baseline"}) <= set(payload["failed_checks"])
    assert payload["checks"]["phase0_baseline"]["status"] == "pass"


def test_gate_rejects_evidence_for_a_different_frozen_contract(
    phase0_manifest, contract_manifest
):
    evidence = complete_fake_evidence()
    evidence["checks"]["contract_digest"]["detail"]["digest"] = "b" * 64
    report = evaluate(
        evidence,
        phase0_manifest=phase0_manifest,
        contract_manifest=contract_manifest,
    )
    assert not report.ok
    assert "contract_digest" in report.failed_checks
    assert any("does not match" in reason for reason in report.reasons)


def test_gate_rejects_pass_without_detail():
    evidence = complete_fake_evidence()
    evidence["checks"]["fifo"] = {"status": "pass"}
    report = evaluate(evidence)
    assert not report.ok
    assert "fifo" in report.failed_checks
    assert any("traceable" in reason or "detail" in reason for reason in report.reasons)


def test_gate_rejects_detail_without_traceable_identifier():
    evidence = complete_fake_evidence()
    evidence["checks"]["idempotency"] = {
        "status": "pass",
        "detail": "checked, all good",
    }
    report = evaluate(evidence)
    assert not report.ok
    assert "idempotency" in report.failed_checks
    assert any("traceable" in reason for reason in report.reasons)


def test_gate_rejects_identifier_kind_outside_whitelist():
    evidence = complete_fake_evidence()
    # audit 只认 command_id/digest；只给 duration 不算可追溯。
    evidence["checks"]["audit"] = {"status": "pass", "detail": {"duration_seconds": 3}}
    report = evaluate(evidence)
    assert not report.ok
    assert "audit" in report.failed_checks


def test_phase0_baseline_requires_accepted_manifest():
    evidence = complete_fake_evidence()
    report = evaluate(evidence, phase0_manifest=None)
    assert not report.ok
    assert "phase0_baseline" in report.failed_checks
    assert any("phase0" in reason for reason in report.reasons)


def test_phase0_baseline_rejects_manifest_not_accepted(tmp_path):
    manifest = tmp_path / "phase0" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema_version": 1, "accepted": False}))
    evidence = complete_fake_evidence()
    report = evaluate(evidence, phase0_manifest=manifest)
    assert not report.ok
    assert "phase0_baseline" in report.failed_checks


def test_phase0_baseline_ignores_evidence_claim(tmp_path, phase0_manifest):
    evidence = complete_fake_evidence()
    evidence["checks"]["phase0_baseline"] = {
        "status": "pass",
        "detail": {"contract_digest": _HEX64},
    }
    # phase0_baseline 只认 manifest 文件；evidence 里的自述不能替代。
    report = evaluate(evidence, phase0_manifest=None)
    assert not report.ok
    assert "phase0_baseline" in report.failed_checks
