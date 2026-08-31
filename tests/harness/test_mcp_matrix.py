from __future__ import annotations

import asyncio

from ksadk.harness.capabilities import CapabilityDescriptor, CapabilityKind
from ksadk.harness.mcp_matrix import (
    McpMatrixCandidate,
    McpProbe,
    McpResilienceCase,
    run_mcp_matrix,
)
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


class _FlakyTransport:
    """企业故障注入：每次调用悬挂 hang_seconds，用于验证截止时间与恢复。"""

    def __init__(self, *, hang_seconds: float = 2.0) -> None:
        self.hang_seconds = hang_seconds
        self.failures = 0
        self.schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }

    async def list_tools(self):  # type: ignore[no-untyped-def]
        return [
            {
                "name": "lookup",
                "description": "read-only lookup",
                "inputSchema": self.schema,
            }
        ]

    async def call_tool(self, name, arguments):  # type: ignore[no-untyped-def]
        del name, arguments
        self.failures += 1
        await asyncio.sleep(self.hang_seconds)
        return {"ok": True}


class _SchemaDriftTransport(_FlakyTransport):
    """企业侧 Schema 变化：每次 tools/list 返回扩大的 inputSchema。"""

    def __init__(self) -> None:
        super().__init__(hang_seconds=0.0)
        self.list_count = 0

    async def list_tools(self):  # type: ignore[no-untyped-def]
        self.list_count += 1
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }
        if self.list_count > 1:
            schema["properties"][f"field_v{self.list_count}"] = {"type": "string"}
        return [
            {
                "name": "lookup",
                "description": "read-only lookup",
                "inputSchema": schema,
            }
        ]


def _flaky_runtime(transport: _FlakyTransport) -> tuple[McpCapabilityRuntime, str]:
    server_id = "mcp://enterprise-erp@1.0.0"
    runtime = McpCapabilityRuntime(
        options=McpRuntimeOptions(
            health_ttl_seconds=0,
            failure_threshold=2,
            call_timeout_seconds=10.0,
        )
    )
    runtime.bind(
        McpServerBinding(
            descriptor=CapabilityDescriptor(
                id=server_id,
                name="enterprise-erp",
                kind=CapabilityKind.MCP,
                version="1.0.0",
            ),
            transport=transport,
            credential_ref="secret://erp",
        )
    )
    return runtime, server_id


def test_matrix_timeout_probe_hangs_and_recovery_stays_classified():
    transport = _FlakyTransport(hang_seconds=0.3)
    runtime, server_id = _flaky_runtime(transport)

    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id=server_id,
                    runtime=runtime,
                    probe=McpProbe(tool_name="lookup", arguments={"q": "po"}),
                    resilience=McpResilienceCase(
                        timeout_seconds=0.2,
                        verify_disconnect_recovery=True,
                    ),
                ),
            )
        )
    )

    rules = {item["rule"]: item for item in report.rows[0].findings}
    assert rules["mcp.resilience.timeout"]["status"] == "failed"
    assert rules["mcp.resilience.disconnect_recovery"]["status"] == "passed"
    assert "availability available -> available" in rules["mcp.resilience.disconnect_recovery"][
        "detail"
    ]


def test_matrix_timeout_probe_classified_failure_passes():
    # runtime 层超时（0.05s）先于 matrix deadline（5s）触发：失败必须被分类。
    runtime, server_id = _flaky_runtime(_FlakyTransport(hang_seconds=0.3))
    runtime._options = McpRuntimeOptions(
        health_ttl_seconds=0,
        failure_threshold=2,
        call_timeout_seconds=0.05,
    )

    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id=server_id,
                    runtime=runtime,
                    probe=McpProbe(tool_name="lookup", arguments={"q": "po"}),
                    resilience=McpResilienceCase(timeout_seconds=5.0),
                ),
            )
        )
    )

    rules = {item["rule"]: item for item in report.rows[0].findings}
    assert rules["mcp.resilience.timeout"]["status"] == "passed"


def test_matrix_schema_refresh_detects_server_schema_change():
    runtime, server_id = _flaky_runtime(_SchemaDriftTransport())

    report = asyncio.run(
        run_mcp_matrix(
            (
                McpMatrixCandidate(
                    server_id=server_id,
                    runtime=runtime,
                    probe=McpProbe(tool_name="lookup", arguments={"q": "po"}),
                    resilience=McpResilienceCase(verify_schema_refresh=True),
                ),
            )
        )
    )

    rules = {item["rule"]: item for item in report.rows[0].findings}
    refresh = rules["mcp.tools.schema_refresh"]
    assert refresh["status"] == "passed"
    assert "schema_changed=true" in refresh["detail"]


def test_invalid_resilience_timeout_is_rejected():
    try:
        McpResilienceCase(timeout_seconds=0)
    except ValueError as exc:
        assert "timeout_seconds" in str(exc)
    else:
        raise AssertionError("non-positive timeout must be rejected")
