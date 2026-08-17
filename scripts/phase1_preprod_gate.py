# -*- coding: utf-8 -*-
"""Phase 1 预发 release gate（Task 13 Step 1/10）。

聚合各 closed-loop 演练产出的 evidence JSON，输出机器可读 pass/fail report：

- required check 集合固定（见 ``REQUIRED_CHECKS``），任何一个 missing /
  skipped / failed 都让 gate FAIL。
- evidence / report 中不允许出现 DSN、Secret、authorization token 或用户
  prompt 原文；gate 对常见泄露形态做启发式扫描并在发现时 FAIL。
- report 本体只保存 resource ref / digest / 计数，不保存凭据。

用法::

    uv run python scripts/phase1_preprod_gate.py \
        --environment pre \
        --output docs/superpowers/evidence/phase1/preprod-report.json \
        --evidence docs/superpowers/evidence/phase1/*.json

本脚本不做任何部署 / helm / kubectl 操作；它只消费已存在的 evidence 文件。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_CHECKS: frozenset[str] = frozenset(
    {
        "contract_digest",
        "fifo",
        "idempotency",
        "queue_full",
        "reconnect",
        "cold_recovery",
        "stale_fence",
        "audit",
        "cross_repo_versions",
        "rollback",
    }
)

# evidence 值里出现这些模式即视为疑似凭据/DSN 泄露。
FORBIDDEN_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"postgres(ql)?://", re.IGNORECASE),
    re.compile(r"mysql://", re.IGNORECASE),
    re.compile(r"redis://", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\b(sk|pk|api)[-_]?(key|token)\b\s*[:=]", re.IGNORECASE),
)
# evidence 键名里出现这些词即视为泄露（refs 除外，ref 指 resource 引用）。
FORBIDDEN_KEY_TOKENS: tuple[str, ...] = (
    "dsn",
    "secret",
    "password",
    "authorization",
    "api_key",
    "access_token",
    "prompt_text",
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class GateReport:
    """机器可读 gate 结果。passed/skipped/failed 是 check name 集合。"""

    environment: str
    scenario: str
    status: str  # "pass" | "fail"
    passed_checks: frozenset[str]
    skipped_checks: frozenset[str]
    failed_checks: frozenset[str]
    reasons: tuple[str, ...] = ()
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_json(self) -> str:
        return json.dumps(
            {
                "environment": self.environment,
                "scenario": self.scenario,
                "generated_at": _now_iso(),
                "status": self.status,
                "passed_checks": sorted(self.passed_checks),
                "skipped_checks": sorted(self.skipped_checks),
                "failed_checks": sorted(self.failed_checks),
                "reasons": list(self.reasons),
                "checks": self.checks,
            },
            indent=2,
            sort_keys=True,
        )


def _scan_forbidden(value: Any, *, path: str = "") -> list[str]:
    """递归扫描 evidence，返回疑似 DSN/Secret/token 的位置描述。"""

    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if any(token in lowered for token in FORBIDDEN_KEY_TOKENS):
                findings.append(f"forbidden key '{key_path}'")
            findings.extend(_scan_forbidden(item, path=key_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_scan_forbidden(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(value):
                findings.append(f"forbidden value at '{path}'")
                break
    return findings


def evaluate_evidence(
    evidence: dict[str, Any], *, environment: str, scenario: str
) -> GateReport:
    """把 evidence dict 折叠成 GateReport。

    规则（与 Task 13 Step 1 验收一致）：
    - required check 缺失 -> failed（reason: missing）。
    - required check 为 skipped -> failed（gate 反向检查：required 不能 skip）。
    - required check 状态非 pass -> failed。
    - 非 required check 允许 skip/缺失，不计入 fail。
    - 任何 forbidden 字段 -> 整体 fail。
    """

    checks: dict[str, dict[str, Any]] = dict(evidence.get("checks") or {})
    passed: set[str] = set()
    skipped: set[str] = set()
    failed: set[str] = set()
    reasons: list[str] = []

    for name in sorted(REQUIRED_CHECKS):
        detail = checks.get(name)
        if detail is None:
            failed.add(name)
            reasons.append(f"required check '{name}' missing from evidence")
            continue
        status = str(detail.get("status", "")).lower()
        if status == "pass":
            passed.add(name)
        elif status == "skip" or status == "skipped":
            skipped.add(name)
            reasons.append(f"required check '{name}' was skipped")
        else:
            failed.add(name)
            reasons.append(
                f"required check '{name}' status '{status or 'unknown'}'"
            )

    for name, detail in sorted(checks.items()):
        status = str(detail.get("status", "")).lower()
        if name not in REQUIRED_CHECKS:
            if status == "pass":
                passed.add(name)
            elif status in ("skip", "skipped"):
                skipped.add(name)

    findings = _scan_forbidden(checks)
    if findings:
        reasons.append(
            "evidence contains forbidden secret-shaped fields: "
            + "; ".join(findings[:5])
        )

    # skipped 与 required 相交即 fail（即使上面已经逐条记 failed）。
    if skipped & set(REQUIRED_CHECKS):
        failed |= skipped & set(REQUIRED_CHECKS)

    status = "pass" if not failed and not findings else "fail"
    return GateReport(
        environment=environment,
        scenario=scenario,
        status=status,
        passed_checks=frozenset(passed),
        skipped_checks=frozenset(skipped),
        failed_checks=frozenset(failed),
        reasons=tuple(reasons),
        checks={name: dict(detail) for name, detail in checks.items()},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate Phase 1 preprod closure evidence into a pass/fail gate report."
    )
    parser.add_argument("--environment", default="pre", help="target environment label")
    parser.add_argument(
        "--scenario",
        default="full_closure",
        help="evidence scenario label (e.g. rollback, full_closure)",
    )
    parser.add_argument(
        "--evidence",
        action="append",
        default=None,
        help="path(s) to evidence JSON files; may be passed multiple times",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="path to write the machine-readable gate report JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    merged: dict[str, Any] = {"checks": {}}
    evidence_paths = [Path(p) for p in (args.evidence or [])]
    for path in evidence_paths:
        if not path.exists():
            print(f"gate: evidence file missing: {path}", file=sys.stderr)
            merged["checks"][path.stem] = {"status": "missing"}
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name, detail in (payload.get("checks") or {}).items():
            merged["checks"].setdefault(name, detail)

    report = evaluate_evidence(
        merged, environment=args.environment, scenario=args.scenario
    )

    text = report.to_json()
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"gate: report written to {target}")
    print(text)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
