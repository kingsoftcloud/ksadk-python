from __future__ import annotations

import asyncio

from ksadk.harness.sandbox_backend import (
    LocalReadOnlySandboxBackend,
    SandboxSpec,
    SubprocessSandboxBackend,
)
from ksadk.harness.sandbox_conformance import SandboxConformanceCase
from ksadk.harness.sandbox_matrix import SandboxMatrixCandidate, run_sandbox_matrix


def test_matrix_distinguishes_ready_warning_and_unconfigured(tmp_path):
    subprocess = SubprocessSandboxBackend(base_dir=tmp_path / "sandboxes")
    read_only = LocalReadOnlySandboxBackend()
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")

    report = asyncio.run(
        run_sandbox_matrix(
            (
                SandboxMatrixCandidate(
                    backend_id="local-subprocess",
                    backend=subprocess,
                    spec=SandboxSpec(workspace_root="", read_only=False),
                    case=SandboxConformanceCase(
                        smoke_command="printf ok",
                        expected_output="ok",
                        timeout_command="sleep 1",
                        artifact_command="printf artifact > result.txt",
                        expected_artifact="result.txt",
                    ),
                    cancellation_command="sleep 5",
                ),
                SandboxMatrixCandidate(
                    backend_id="local-read-only",
                    backend=read_only,
                    spec=SandboxSpec(workspace_root=str(tmp_path), read_only=True),
                    case=SandboxConformanceCase(
                        smoke_command="cat hello.txt", expected_output="hello"
                    ),
                ),
                SandboxMatrixCandidate(
                    backend_id="e2b",
                    backend=None,
                    spec=SandboxSpec(workspace_root="", read_only=False),
                    case=SandboxConformanceCase(smoke_command="true"),
                    unavailable_reason="KSADK_REAL_SANDBOX_E2E is not enabled",
                ),
            )
        )
    )

    rows = {row.backend_id: row for row in report.rows}
    assert rows["local-subprocess"].status == "ready"
    assert rows["local-read-only"].status == "warning"
    assert rows["e2b"].status == "not_configured"
    assert report.status == "warning"
    wire = report.to_dict()
    assert wire["schemaVersion"] == 1
    assert wire["rows"][0]["capabilities"]["filesystem_isolation"]


def test_required_remote_backend_blocks_matrix():
    report = asyncio.run(
        run_sandbox_matrix(
            (
                SandboxMatrixCandidate(
                    backend_id="private-cloud",
                    backend=None,
                    required=True,
                    spec=SandboxSpec(workspace_root="", read_only=False),
                    case=SandboxConformanceCase(smoke_command="true"),
                    unavailable_reason="api_key=do-not-leak endpoint is missing",
                ),
            )
        )
    )

    assert report.status == "blocked"
    assert report.rows[0].summary == {"passed": 0, "failed": 1, "skipped": 0}
    assert "do-not-leak" not in report.rows[0].findings[0]["detail"]
