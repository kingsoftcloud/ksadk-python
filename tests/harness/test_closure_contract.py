from __future__ import annotations

from ksadk.harness.closure_contract import (
    FailureCategory,
    HarnessClosureReport,
    ReadinessCheck,
    ReadinessStatus,
    RunTerminalStatus,
)


def test_report_round_trip_preserves_not_configured_as_a_distinct_state():
    report = HarnessClosureReport(
        target="e2b",
        status=ReadinessStatus.NOT_CONFIGURED,
        checks=(
            ReadinessCheck(
                check_id="sandbox-e2b",
                category="sandbox",
                status=ReadinessStatus.NOT_CONFIGURED,
                required=False,
                reason_code="template_not_configured",
            ),
        ),
    )

    payload = report.model_dump(mode="json", by_alias=True)

    assert payload["schemaVersion"] == 1
    assert payload["status"] == "not_configured"
    assert payload["counts"] == {
        "ready": 0,
        "warning": 0,
        "blocked": 0,
        "not_configured": 1,
    }
    assert HarnessClosureReport.model_validate(payload) == report


def test_report_reader_ignores_additive_fields_from_a_newer_writer():
    payload = {
        "schemaVersion": 1,
        "target": "managed-langgraph",
        "status": "ready",
        "checks": [],
        "counts": {"ready": 0, "warning": 0, "blocked": 0, "not_configured": 0},
        "futureEvidence": {"opaque": True},
    }

    parsed = HarnessClosureReport.model_validate(payload)

    assert parsed.status is ReadinessStatus.READY
    assert "futureEvidence" not in parsed.model_dump(by_alias=True)


def test_terminal_status_normalizes_legacy_canceled_spelling():
    assert RunTerminalStatus.normalize("canceled") is RunTerminalStatus.CANCELLED
    assert RunTerminalStatus.normalize("awaiting_approval") is (
        RunTerminalStatus.AWAITING_APPROVAL
    )


def test_failure_category_is_structured_without_raw_error_text():
    failure = FailureCategory(
        domain="provider",
        code="context_length",
        retryable=False,
    )

    assert failure.model_dump(mode="json") == {
        "domain": "provider",
        "code": "context_length",
        "retryable": False,
    }
