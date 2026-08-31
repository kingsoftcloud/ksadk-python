from __future__ import annotations

import asyncio

from ksadk.harness.capabilities import CapabilityDescriptor, CapabilityKind
from ksadk.harness.mcp_matrix import McpMatrixCandidate, McpProbe, run_mcp_matrix
from ksadk.harness.mcp_runtime import (
    McpAuthenticationError,
    McpCapabilityRuntime,
    McpRuntimeOptions,
    McpServerBinding,
)


class _Transport:
    def __init__(self, *, authenticated: bool = True) -> None:
        self.authenticated = authenticated

    async def list_tools(self):  # type: ignore[no-untyped-def]
        if not self.authenticated:
            raise McpAuthenticationError("authorization=should-not-leak")
        return [
            {
                "name": "lookup",
                "description": "read-only lookup",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {"name": "invalid", "description": "missing schema"},
        ]

    async def call_tool(self, name, arguments):  # type: ignore[no-untyped-def]
        return {"secret": "must-not-enter-report", "name": name, "args": arguments}


class _Refresher:
    async def refresh(self, *, server_id, transport):  # type: ignore[no-untyped-def]
        transport.authenticated = True
        return True


def _runtime(*, rotate: bool = False) -> tuple[McpCapabilityRuntime, str]:
    server_id = "mcp://finance@1.0.0"
    runtime = McpCapabilityRuntime(
        options=McpRuntimeOptions(health_ttl_seconds=0, failure_threshold=2)
    )
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=server_id,
                name="finance",
                kind=CapabilityKind.MCP,
                version="1.0.0",
            ),
            transport=_Transport(authenticated=not rotate),
            credential_ref="secret://finance",
            credential_refresher=_Refresher() if rotate else None,
        )
    )
    return runtime, server_id


def test_matrix_runs_health_discovery_and_safe_probe_without_result_leakage():
    runtime, server_id = _runtime()

    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id=server_id,
                    runtime=runtime,
                    probe=McpProbe(tool_name="lookup", arguments={"q": "budget"}),
                ),
            )
        )
    )

    row = report.rows[0]
    assert row.status == "warning"  # rotation is intentionally not probed
    assert row.capabilities["validToolCount"] == 1
    assert row.capabilities["availability"] == "available"
    assert row.summary == {"passed": 3, "failed": 0, "skipped": 1}
    wire = str(report.to_dict())
    assert "must-not-enter-report" not in wire
    assert report.to_dict()["schemaVersion"] == 1


def test_matrix_observes_auth_rotation_without_secret_material():
    runtime, server_id = _runtime(rotate=True)

    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id=server_id,
                    runtime=runtime,
                    require_credential_rotation=True,
                ),
            )
        )
    )

    row = report.rows[0]
    assert row.capabilities["credentialGeneration"] == 1
    assert next(
        item for item in row.findings if item["rule"] == "mcp.credential.rotation"
    )["status"] == "passed"
    assert "should-not-leak" not in str(report.to_dict())


def test_matrix_keeps_unconfigured_enterprise_server_explicit_and_redacted():
    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id="mcp://enterprise@1.0.0",
                    runtime=None,
                    required=True,
                    unavailable_reason="api_key=do-not-leak endpoint unavailable",
                ),
            )
        )
    )

    assert report.status == "blocked"
    assert report.rows[0].status == "blocked"
    assert "do-not-leak" not in str(report.to_dict())


def test_invalid_minimum_tool_count_is_rejected():
    try:
        McpMatrixCandidate(
            server_id="mcp://finance@1.0.0", runtime=None, minimum_valid_tools=-1
        )
    except ValueError as exc:
        assert "minimum_valid_tools" in str(exc)
    else:
        raise AssertionError("negative minimum must be rejected")
