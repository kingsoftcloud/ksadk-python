from __future__ import annotations

from ksadk.harness.release_readiness import ReleaseEvidence, release_readiness


class _Report:
    def __init__(self, status: str) -> None:
        self.status = status

    def to_dict(self):  # type: ignore[no-untyped-def]
        return {
            "schemaVersion": 1,
            "status": self.status,
            "summary": {"passed": 2, "failed": 0, "skipped": 1},
        }


def test_release_gate_aggregates_public_reports_and_keeps_fixes_actionable():
    report = release_readiness(
        {"status": "ready", "counts": {"ready": 2, "warning": 0}},
        (
            ReleaseEvidence("models", "model", _Report("ready")),
            ReleaseEvidence("mcp-finance", "mcp", _Report("warning")),
        ),
    )

    assert report["status"] == "warning"
    assert report["deployable"] is True
    assert report["counts"] == {"ready": 2, "warning": 1, "blocked": 0}
    assert report["fixes"] == [
        {
            "check_id": "mcp-finance",
            "category": "mcp",
            "severity": "warning",
            "action": "检查 MCP 连接、鉴权、Schema 与必需 Tool",
        }
    ]


def test_required_blocker_prevents_deployment():
    report = release_readiness(
        {"status": "ready"},
        (ReleaseEvidence("sandbox-prod", "sandbox", _Report("blocked")),),
    )

    assert report["status"] == "blocked"
    assert report["deployable"] is False


def test_optional_external_matrix_downgrades_blocker_to_warning():
    report = release_readiness(
        {"status": "ready"},
        (
            ReleaseEvidence(
                "e2b",
                "sandbox",
                {"status": "blocked", "summary": {"failed": 1}},
                required=False,
            ),
        ),
    )

    check = report["checks"][1]
    assert check["source_status"] == "blocked"
    assert check["status"] == "warning"
    assert report["deployable"] is True


def test_unknown_status_never_silently_becomes_ready():
    report = release_readiness({"status": "not_configured"})

    assert report["status"] == "warning"
    assert report["checks"][0]["reason_code"] == "runtime_warning"
