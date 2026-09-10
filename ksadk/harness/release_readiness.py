"""Harness 发布准入聚合合同。

聚合 Runtime smoke、模型兼容矩阵、MCP 矩阵与 Sandbox 矩阵的公开报告。
本模块不解析各实现的私有对象，因而可同时被 SDK、CI 与 Studio 使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


class _SerializableReport(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReleaseEvidence:
    evidence_id: str
    category: str
    report: Mapping[str, Any] | _SerializableReport
    required: bool = True


def release_readiness(
    runtime_report: Mapping[str, Any],
    evidence: Sequence[ReleaseEvidence] = (),
) -> dict[str, Any]:
    """生成 ``ready / warning / blocked`` 的发布门禁快照。"""

    checks: list[dict[str, Any]] = []
    runtime_source_status = _status(runtime_report.get("status"))
    runtime_status = _effective_status(runtime_source_status, required=True)
    checks.append(
        {
            "id": "runtime",
            "category": "runtime",
            "status": runtime_status,
            "source_status": runtime_source_status,
            "required": True,
            "reason_code": _reason("runtime", runtime_status, runtime_source_status),
            "summary": _summary(runtime_report),
        }
    )

    for item in evidence:
        payload = (
            item.report.to_dict()
            if callable(getattr(item.report, "to_dict", None))
            else dict(item.report)
        )
        source_status = _status(payload.get("status"))
        effective = _effective_status(source_status, required=item.required)
        checks.append(
            {
                "id": item.evidence_id,
                "category": item.category,
                "status": effective,
                "source_status": source_status,
                "required": item.required,
                "reason_code": _reason(item.category, effective, source_status),
                "summary": _summary(payload),
            }
        )

    overall = "ready"
    if any(check["status"] == "blocked" for check in checks):
        overall = "blocked"
    elif any(check["status"] == "warning" for check in checks):
        overall = "warning"

    fixes = [_fix(check) for check in checks if check["status"] != "ready"]
    return {
        "schemaVersion": 1,
        "status": overall,
        "deployable": overall != "blocked",
        "checks": checks,
        "fixes": fixes,
        "counts": {
            state: sum(1 for check in checks if check["status"] == state)
            for state in ("ready", "warning", "blocked")
        },
    }


def _status(value: object) -> str:
    normalized = str(value or "warning").lower()
    if normalized in {"ready", "warning", "blocked", "not_configured"}:
        return normalized
    if normalized in {"passed", "healthy", "available"}:
        return "ready"
    if normalized in {"failed", "unhealthy", "error"}:
        return "blocked"
    return "warning"


def _effective_status(source_status: str, *, required: bool) -> str:
    if source_status == "not_configured":
        return "blocked" if required else "warning"
    if not required and source_status == "blocked":
        return "warning"
    return source_status


def _summary(report: Mapping[str, Any]) -> dict[str, int]:
    raw = report.get("counts") or report.get("summary") or {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            result[str(key)] = value
    return result


def _reason(category: str, status: str, source_status: str) -> str:
    return f"{category}_{source_status if source_status == 'not_configured' else status}"


def _fix(check: Mapping[str, Any]) -> dict[str, Any]:
    category = str(check["category"])
    suggestions = {
        "runtime": "运行一次 Draft/Smoke 会话并修复阻断项",
        "model": "配置并验证至少一个可用 Model Profile",
        "mcp": "检查 MCP 连接、鉴权、Schema 与必需 Tool",
        "skill": "固定 Skill 版本并确保必需内容包已下载且 Manifest 有效",
        "sandbox": "配置目标 Sandbox Backend 并运行 Conformance",
        "lifecycle": "验证构建、部署、激活、调用与回滚链路",
    }
    return {
        "check_id": check["id"],
        "category": category,
        "severity": check["status"],
        "action": suggestions.get(category, "查看检查证据并修复不兼容项"),
    }


__all__ = ["ReleaseEvidence", "release_readiness"]
