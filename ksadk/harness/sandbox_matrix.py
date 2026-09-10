"""统一 Sandbox Backend 就绪矩阵。

矩阵不替厂商后端伪造通过结果。调用方把已装配的 Backend 与探针传入；未
装配的 E2B/KOP/私有后端必须显式报告 ``not_configured``。Studio/CI 可以
据此区分 ready、warning 与 blocked，并保留每条 Conformance 证据。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ksadk.harness.sandbox_backend import SandboxBackend, SandboxSpec
from ksadk.harness.sandbox_conformance import (
    SandboxConformanceCase,
    run_sandbox_backend_conformance,
    verify_cooperative_cancellation,
)

_SECRET_DETAIL = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
)


def _safe_detail(value: str) -> str:
    """报告只保留诊断摘要，避免厂商异常把凭证带入 Studio/CI。"""

    return _SECRET_DETAIL.sub(r"\1=[REDACTED]", str(value or ""))[:512]


@dataclass(frozen=True)
class SandboxMatrixCandidate:
    backend_id: str
    backend: SandboxBackend | None
    spec: SandboxSpec
    case: SandboxConformanceCase
    required: bool = False
    unavailable_reason: str = "not configured"
    cancellation_command: str | None = None


@dataclass(frozen=True)
class SandboxMatrixRow:
    backend_id: str
    status: str
    required: bool
    capabilities: dict[str, Any] = field(default_factory=dict)
    findings: tuple[dict[str, str], ...] = ()
    summary: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxMatrixReport:
    schema_version: int
    status: str
    rows: tuple[SandboxMatrixRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "rows": [
                {
                    "backendId": row.backend_id,
                    "status": row.status,
                    "required": row.required,
                    "capabilities": dict(row.capabilities),
                    "findings": [dict(item) for item in row.findings],
                    "summary": dict(row.summary),
                }
                for row in self.rows
            ],
        }


async def run_sandbox_matrix(
    candidates: tuple[SandboxMatrixCandidate, ...],
) -> SandboxMatrixReport:
    """运行全部已装配后端；未装配项保留为可审计行。"""

    rows: list[SandboxMatrixRow] = []
    for candidate in candidates:
        if candidate.backend is None:
            status = "blocked" if candidate.required else "not_configured"
            rows.append(
                SandboxMatrixRow(
                    backend_id=candidate.backend_id,
                    status=status,
                    required=candidate.required,
                    findings=(
                        {
                            "rule": "backend.configuration",
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

        backend = candidate.backend
        report = await run_sandbox_backend_conformance(
            backend,
            spec=candidate.spec,
            case=candidate.case,
        )
        findings = [
            {"rule": item.rule, "status": item.status, "detail": _safe_detail(item.detail)}
            for item in report.findings
        ]
        if candidate.cancellation_command:
            cancellation = await verify_cooperative_cancellation(
                backend,
                spec=candidate.spec,
                command=candidate.cancellation_command,
            )
            findings.extend(
                {
                    "rule": item.rule,
                    "status": item.status,
                    "detail": _safe_detail(item.detail),
                }
                for item in cancellation.findings
            )
        counts = {
            state: sum(1 for item in findings if item["status"] == state)
            for state in ("passed", "failed", "skipped")
        }
        if counts["failed"]:
            status = "blocked"
        elif counts["skipped"]:
            status = "warning"
        else:
            status = "ready"
        capabilities = asdict(backend.capabilities)
        capabilities = {
            key: value.value if hasattr(value, "value") else value
            for key, value in capabilities.items()
        }
        rows.append(
            SandboxMatrixRow(
                backend_id=candidate.backend_id,
                status=status,
                required=candidate.required,
                capabilities=capabilities,
                findings=tuple(findings),
                summary=counts,
            )
        )

    overall = "ready"
    if any(row.status == "blocked" for row in rows):
        overall = "blocked"
    elif any(row.status in {"warning", "not_configured"} for row in rows):
        overall = "warning"
    return SandboxMatrixReport(schema_version=1, status=overall, rows=tuple(rows))


__all__ = [
    "SandboxMatrixCandidate",
    "SandboxMatrixReport",
    "SandboxMatrixRow",
    "run_sandbox_matrix",
]
