"""Skill Runtime 解析与发布就绪矩阵。

这里只检查 Revision 已绑定 Skill 的运行时消费条件，不承担安装、升级、市场
推荐或版本治理。报告可供 CI/Studio 使用，并刻意不包含 Skill 正文、本地绝对
路径或缓存实现细节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ksadk.harness.skill_composition import (
    SkillResolutionError,
    parse_skill_ref,
    resolve_skill_roots,
)
from ksadk.harness.spec import HarnessSpec
from ksadk.skills.loader import load_local_skill


@dataclass(frozen=True)
class SkillMatrixRow:
    skill_ref: str
    status: str
    required: bool
    load_policy: str
    findings: tuple[dict[str, str], ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillMatrixReport:
    schema_version: int
    status: str
    rows: tuple[SkillMatrixRow, ...]

    def to_dict(self) -> dict[str, Any]:
        counts = {
            state: sum(1 for row in self.rows if row.status == state)
            for state in ("ready", "warning", "blocked")
        }
        return {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "counts": counts,
            "rows": [
                {
                    "skillRef": row.skill_ref,
                    "status": row.status,
                    "required": row.required,
                    "loadPolicy": row.load_policy,
                    "findings": [dict(item) for item in row.findings],
                    "capabilities": dict(row.capabilities),
                }
                for row in self.rows
            ],
        }


def run_skill_matrix(
    spec: HarnessSpec,
    *,
    explicit_roots: dict[str, str | Path] | None = None,
    local_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> SkillMatrixReport:
    """检查 Skill 引用、运行时内容解析与 Manifest 可加载性。"""

    rows: list[SkillMatrixRow] = []
    for binding in spec.capabilities.skill_bindings:
        ref = binding.capability_ref
        findings: list[dict[str, str]] = []
        try:
            name, version = parse_skill_ref(ref)
        except SkillResolutionError as exc:
            rows.append(
                _failed_row(binding, "skill.reference", str(exc))
            )
            continue

        fixed = bool(version)
        findings.append(
            {
                "rule": "skill.version.pinned",
                "status": "passed" if fixed else "failed",
                "detail": "fixed version reference" if fixed else "version is not pinned",
            }
        )

        if binding.load_policy == "explicit":
            findings.append(
                {
                    "rule": "skill.runtime.visibility",
                    "status": "passed",
                    "detail": "explicit binding is hidden from the model loop",
                }
            )
            status = "ready" if fixed else ("blocked" if binding.required else "warning")
            rows.append(
                SkillMatrixRow(
                    skill_ref=ref,
                    status=status,
                    required=binding.required,
                    load_policy=binding.load_policy,
                    findings=tuple(findings),
                    capabilities={"resolved": False, "manifestValid": False, "name": name},
                )
            )
            continue

        single_spec = spec.model_copy(
            update={
                "capabilities": spec.capabilities.model_copy(
                    update={"skill_bindings": (binding,)}
                )
            }
        )
        try:
            roots, warnings = resolve_skill_roots(
                single_spec,
                explicit_roots=explicit_roots,
                local_dir=local_dir,
                cache_dir=cache_dir,
            )
        except SkillResolutionError as exc:
            findings.append(
                {"rule": "skill.content.resolution", "status": "failed", "detail": _safe(exc)}
            )
            rows.append(_row(binding, findings, name=name, resolved=False))
            continue

        root = roots.get(ref)
        if root is None:
            findings.append(
                {
                    "rule": "skill.content.resolution",
                    "status": "failed",
                    "detail": _safe(warnings[0] if warnings else "content unavailable"),
                }
            )
            rows.append(_row(binding, findings, name=name, resolved=False))
            continue

        findings.append(
            {
                "rule": "skill.content.resolution",
                "status": "passed",
                "detail": "validated local content resolved",
            }
        )
        try:
            loaded = load_local_skill(root)
        except Exception as exc:  # noqa: BLE001 - stable matrix failure
            findings.append(
                {"rule": "skill.manifest", "status": "failed", "detail": _safe(exc)}
            )
            rows.append(_row(binding, findings, name=name, resolved=True))
            continue
        findings.append(
            {
                "rule": "skill.manifest",
                "status": "passed",
                "detail": "manifest loaded",
            }
        )
        rows.append(
            _row(
                binding,
                findings,
                name=loaded.name,
                resolved=True,
                manifest_valid=True,
            )
        )

    overall = "ready"
    if any(row.status == "blocked" for row in rows):
        overall = "blocked"
    elif any(row.status == "warning" for row in rows):
        overall = "warning"
    return SkillMatrixReport(schema_version=1, status=overall, rows=tuple(rows))


def _row(
    binding: Any,
    findings: list[dict[str, str]],
    *,
    name: str,
    resolved: bool,
    manifest_valid: bool = False,
) -> SkillMatrixRow:
    failed = any(item["status"] == "failed" for item in findings)
    status = ("blocked" if binding.required else "warning") if failed else "ready"
    return SkillMatrixRow(
        skill_ref=binding.capability_ref,
        status=status,
        required=binding.required,
        load_policy=binding.load_policy,
        findings=tuple(findings),
        capabilities={
            "resolved": resolved,
            "manifestValid": manifest_valid,
            "progressiveDisclosure": binding.load_policy == "on_demand",
            "name": name,
        },
    )


def _failed_row(binding: Any, rule: str, detail: str) -> SkillMatrixRow:
    return _row(
        binding,
        [{"rule": rule, "status": "failed", "detail": _safe(detail)}],
        name="",
        resolved=False,
    )


def _safe(value: object) -> str:
    # 绝对路径可能暴露宿主目录；报告只保留稳定错误类别和文件名。
    text = str(value or "")[:512]
    for token in text.split():
        if token.startswith("/"):
            text = text.replace(token, Path(token.rstrip(":")).name)
    return text


__all__ = [
    "SkillMatrixReport",
    "SkillMatrixRow",
    "run_skill_matrix",
]
