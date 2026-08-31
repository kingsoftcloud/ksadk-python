"""MCP Server 兼容性与生产就绪矩阵。

矩阵只验证已由宿主装配的 MCP Transport，不负责发现或保存企业凭证。未
配置的远端服务必须显式报告 ``not_configured``；报告只包含能力、计数和
稳定错误分类，不包含 Tool 返回正文或 Secret 引用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.mcp_runtime import McpCapabilityRuntime

_SECRET_DETAIL = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
)


def _safe_detail(value: object) -> str:
    return _SECRET_DETAIL.sub(r"\1=[REDACTED]", str(value or ""))[:512]


@dataclass(frozen=True)
class McpProbe:
    """可选的无副作用 Tool 探针。

    调用方必须只配置可安全重复执行的只读 Tool。矩阵不会猜测任意企业
    Tool 是否可重试，也不会把调用结果写入报告。
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpMatrixCandidate:
    server_id: str
    runtime: McpCapabilityRuntime | None
    required: bool = False
    unavailable_reason: str = "not configured"
    probe: McpProbe | None = None
    minimum_valid_tools: int = 1
    require_credential_rotation: bool = False

    def __post_init__(self) -> None:
        if self.minimum_valid_tools < 0:
            raise ValueError("minimum_valid_tools must be >= 0")


@dataclass(frozen=True)
class McpMatrixRow:
    server_id: str
    status: str
    required: bool
    findings: tuple[dict[str, str], ...] = ()
    summary: dict[str, int] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpMatrixReport:
    schema_version: int
    status: str
    rows: tuple[McpMatrixRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "rows": [
                {
                    "serverId": row.server_id,
                    "status": row.status,
                    "required": row.required,
                    "findings": [dict(item) for item in row.findings],
                    "summary": dict(row.summary),
                    "capabilities": dict(row.capabilities),
                }
                for row in self.rows
            ],
        }


async def run_mcp_matrix(
    candidates: tuple[McpMatrixCandidate, ...],
) -> McpMatrixReport:
    """运行 MCP 健康、发现、Schema、鉴权轮换与可选只读调用探针。"""

    rows: list[McpMatrixRow] = []
    for candidate in candidates:
        if candidate.runtime is None:
            status = "blocked" if candidate.required else "not_configured"
            rows.append(
                McpMatrixRow(
                    server_id=candidate.server_id,
                    status=status,
                    required=candidate.required,
                    findings=(
                        {
                            "rule": "mcp.configuration",
                            "status": "failed" if candidate.required else "skipped",
                            "detail": _safe_detail(candidate.unavailable_reason),
                        },
                    ),
                    summary={
                        "passed": 0,
                        "failed": 1 if candidate.required else 0,
                        "skipped": 0 if candidate.required else 1,
                    },
                )
            )
            continue

        findings: list[dict[str, str]] = []
        runtime = candidate.runtime
        initial_generation = runtime.credential_generation(candidate.server_id)
        health = await runtime.health(candidate.server_id)
        findings.append(
            {
                "rule": "mcp.health",
                "status": "passed" if health.healthy else "failed",
                "detail": "healthy" if health.healthy else _safe_detail(health.reason),
            }
        )

        tools: list[dict[str, Any]] = []
        if health.healthy:
            try:
                runtime.invalidate_tools(candidate.server_id)
                tools = await runtime.tools(candidate.server_id)
            except Exception as exc:  # noqa: BLE001 - matrix records a stable failure
                findings.append(
                    {
                        "rule": "mcp.tools.discovery",
                        "status": "failed",
                        "detail": _safe_detail(exc),
                    }
                )
            else:
                enough = len(tools) >= candidate.minimum_valid_tools
                findings.append(
                    {
                        "rule": "mcp.tools.discovery",
                        "status": "passed" if enough else "failed",
                        "detail": (
                            f"valid_tools={len(tools)}; "
                            f"minimum={candidate.minimum_valid_tools}"
                        ),
                    }
                )
        else:
            findings.append(
                {
                    "rule": "mcp.tools.discovery",
                    "status": "skipped",
                    "detail": "health probe failed",
                }
            )

        if candidate.probe is None:
            findings.append(
                {
                    "rule": "mcp.tool.read_only_probe",
                    "status": "skipped",
                    "detail": "no safe read-only probe configured",
                }
            )
        elif any(tool.get("name") == candidate.probe.tool_name for tool in tools):
            try:
                await runtime.call(
                    candidate.server_id,
                    candidate.probe.tool_name,
                    dict(candidate.probe.arguments),
                )
            except Exception as exc:  # noqa: BLE001
                findings.append(
                    {
                        "rule": "mcp.tool.read_only_probe",
                        "status": "failed",
                        "detail": _safe_detail(exc),
                    }
                )
            else:
                findings.append(
                    {
                        "rule": "mcp.tool.read_only_probe",
                        "status": "passed",
                        "detail": f"tool={candidate.probe.tool_name}",
                    }
                )
        else:
            findings.append(
                {
                    "rule": "mcp.tool.read_only_probe",
                    "status": "failed",
                    "detail": f"tool not discovered: {candidate.probe.tool_name}",
                }
            )

        generation = runtime.credential_generation(candidate.server_id)
        if candidate.require_credential_rotation:
            findings.append(
                {
                    "rule": "mcp.credential.rotation",
                    "status": "passed" if generation > initial_generation else "failed",
                    "detail": (
                        "credential generation advanced"
                        if generation > initial_generation
                        else "credential rotation was required but not observed"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "rule": "mcp.credential.rotation",
                    "status": "skipped",
                    "detail": "rotation probe not required",
                }
            )

        counts = {
            state: sum(1 for item in findings if item["status"] == state)
            for state in ("passed", "failed", "skipped")
        }
        status = "blocked" if counts["failed"] else (
            "warning" if counts["skipped"] else "ready"
        )
        binding = runtime.binding(candidate.server_id)
        rows.append(
            McpMatrixRow(
                server_id=candidate.server_id,
                status=status,
                required=candidate.required,
                findings=tuple(findings),
                summary=counts,
                capabilities={
                    "validToolCount": len(tools),
                    "readOnlyProbeConfigured": candidate.probe is not None,
                    "credentialRotationConfigured": (
                        binding.credential_refresher is not None
                    ),
                    "credentialGeneration": generation,
                    "idempotencyMode": binding.idempotency_mode,
                    "availability": runtime.availability(candidate.server_id),
                },
            )
        )

    overall = "ready"
    if any(row.status == "blocked" for row in rows):
        overall = "blocked"
    elif any(row.status in {"warning", "not_configured"} for row in rows):
        overall = "warning"
    return McpMatrixReport(schema_version=1, status=overall, rows=tuple(rows))


__all__ = [
    "McpMatrixCandidate",
    "McpMatrixReport",
    "McpMatrixRow",
    "McpProbe",
    "run_mcp_matrix",
]
