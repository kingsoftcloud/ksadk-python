# -*- coding: utf-8 -*-
"""Kernel 预发 release gate（Task 13 Step 1/10）。

聚合各 closed-loop 演练产出的 evidence JSON，输出机器可读 pass/fail report：

- required check 集合固定（见 ``REQUIRED_CHECKS``），任何一个 missing /
  skipped / failed 都让 gate FAIL。
- evidence / report 中不允许出现 DSN、Secret、authorization token 或用户
  prompt 原文；gate 对常见泄露形态做启发式扫描并在发现时 FAIL。
- report 本体只保存 resource ref / digest / 计数，不保存凭据。

用法::

    uv run python scripts/kernel_preprod_gate.py \
        --environment pre \
        --output docs/superpowers/evidence/kernel/preprod-report.json \
        --evidence docs/superpowers/evidence/kernel/*.json

本脚本不做任何部署 / helm / kubectl 操作；它只消费已存在的 evidence 文件。
"""

from __future__ import annotations

import argparse
import hashlib
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
        "contract_baseline",
    }
)

# 每个 required check 的 pass 判定必须由带可追溯标识的 detail 支撑。
# 标识种类：command_id / event_id / commit / digest / duration。
TRACE_IDENTIFIERS_BY_CHECK: dict[str, frozenset[str]] = {
    "contract_digest": frozenset({"digest", "commit"}),
    "fifo": frozenset({"event_id", "duration", "digest"}),
    "idempotency": frozenset({"command_id", "event_id"}),
    "queue_full": frozenset({"command_id", "event_id"}),
    "reconnect": frozenset({"event_id", "duration", "digest"}),
    "cold_recovery": frozenset({"event_id", "duration"}),
    "stale_fence": frozenset({"event_id", "command_id"}),
    "audit": frozenset({"command_id", "digest"}),
    "cross_repo_versions": frozenset({"commit", "digest"}),
    "rollback": frozenset({"duration", "commit", "digest"}),
}

_COMMIT_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
_DIGEST_RE = re.compile(r"(?:sha256:)?(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])", re.IGNORECASE)
_EVENT_ID_KEYS = ("event_id", "replay_event_count", "seq", "accepted_seq", "seq_range")
_COMMAND_ID_KEYS = ("command_id",)
_DURATION_KEYS = ("duration_seconds", "rollback_seconds", "rollforward_seconds", "duration", "耗时")

DEFAULT_CONTRACT_MANIFEST = "docs/superpowers/evidence/kernel-contract/manifest.json"
DEFAULT_BASELINE_MANIFEST = "docs/superpowers/evidence/kernel-contract/manifest.json"
DEFAULT_CONTRACT_MANIFEST = "contracts/agent-kernel/v1/manifest.json"

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


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float, bool)):
        return value != 0 or value is False
    return True


def _find_trace_identifiers(value: Any) -> set[str]:
    """递归收集 detail 中出现的可追溯标识种类。"""

    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _COMMAND_ID_KEYS and _truthy(item):
                found.add("command_id")
            if lowered in _EVENT_ID_KEYS and _truthy(item):
                found.add("event_id")
            if (
                lowered in _DURATION_KEYS or lowered.endswith("_seconds")
            ) and _truthy(item):
                found.add("duration")
            if lowered in ("commit", "commit_sha", "revision") and _truthy(item):
                found.add("commit")
            found |= _find_trace_identifiers(item)
    elif isinstance(value, list):
        for item in value:
            found |= _find_trace_identifiers(item)
    elif isinstance(value, str):
        if _DIGEST_RE.search(value):
            found.add("digest")
        if _COMMIT_RE.search(value):
            found.add("commit")
    return found


def _load_contract_manifest(path: Path) -> dict[str, Any] | None:
    """读取 contract baseline manifest；缺失/损坏返回 None（诚实 fail）。"""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_contract_digest(path: Path | None) -> str | None:
    """Read the frozen aggregate contract digest from the checked-out source.

    Evidence is only meaningful for the contract currently being released.  A
    traceable but old drill must not turn green after the aggregate manifest
    has changed.
    """

    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    digest = payload.get("aggregate_digest") if isinstance(payload, dict) else None
    return str(digest).strip() or None


def evaluate_evidence(
    evidence: dict[str, Any],
    *,
    environment: str,
    scenario: str,
    baseline_manifest: Path | None = None,
    contract_manifest: Path | None = None,
) -> GateReport:
    """把 evidence dict 折叠成 GateReport。

    规则（与 Task 13 Step 1 验收一致）：
    - required check 缺失 -> failed（reason: missing）。
    - required check 为 skipped -> failed（gate 反向检查：required 不能 skip）。
    - required check 状态非 pass -> failed。
    - required check 状态为 pass 时必须有非空 detail，且 detail 包含该 check
      白名单内至少一种可追溯标识（command_id/event_id/commit/digest/duration），
      否则视为 invalid -> failed。裸 ``{"status": "pass"}`` 不被信任。
    - ``contract_baseline`` 只能由 contract manifest（accepted=true）支撑，
      evidence 里的自述不能替代。
    - 非 required check 允许 skip/缺失，不计入 fail。
    - 任何 forbidden 字段 -> 整体 fail。
    """

    checks: dict[str, dict[str, Any]] = dict(evidence.get("checks") or {})
    passed: set[str] = set()
    skipped: set[str] = set()
    failed: set[str] = set()
    reasons: list[str] = []

    contract_detail: dict[str, Any] | None = None
    if contract_manifest is None:
        failed.add("contract_baseline")
        reasons.append("contract_baseline requires a manifest path; none was provided")
    else:
        manifest = _load_contract_manifest(baseline_manifest)
        accepted = bool(manifest and manifest.get("accepted") is True)
        if manifest is not None and accepted:
            contract_detail = {
                "manifest": str(baseline_manifest),
                "accepted": True,
                "manifest_digest": hashlib.sha256(
                    baseline_manifest.read_bytes()
                ).hexdigest(),
            }
            passed.add("contract_baseline")
        else:
            failed.add("contract_baseline")
            reasons.append(
                "contract_baseline requires an accepted manifest at "
                f"'{baseline_manifest}' (missing or accepted != true)"
            )
    checks.pop("contract_baseline", None)

    for name in sorted(REQUIRED_CHECKS - {"contract_baseline"}):
        detail = checks.get(name)
        if detail is None:
            failed.add(name)
            reasons.append(f"required check '{name}' missing from evidence")
            continue
        status = str(detail.get("status", "")).lower()
        if status == "pass":
            trace_detail = detail.get("detail")
            allowed = TRACE_IDENTIFIERS_BY_CHECK.get(name, frozenset())
            if not _truthy(trace_detail):
                failed.add(name)
                reasons.append(
                    f"required check '{name}' claims pass without a non-empty detail"
                )
                continue
            identifiers = _find_trace_identifiers(trace_detail)
            if not identifiers & allowed:
                failed.add(name)
                reasons.append(
                    f"required check '{name}' detail lacks a traceable identifier "
                    f"(allowed: {sorted(allowed)}, found: {sorted(identifiers)})"
                )
                continue
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

    expected_contract_digest = _load_contract_digest(contract_manifest)
    if contract_manifest is not None and expected_contract_digest is None:
        for name in ("contract_digest", "cross_repo_versions"):
            passed.discard(name)
            failed.add(name)
        reasons.append(
            f"contract manifest '{contract_manifest}' is missing, invalid, or has no aggregate_digest"
        )
    elif expected_contract_digest is not None:
        contract_detail = checks.get("contract_digest", {}).get("detail") or {}
        version_detail = checks.get("cross_repo_versions", {}).get("detail") or {}
        claimed_contract_digest = (
            contract_detail.get("digest") or contract_detail.get("contract_digest")
            if isinstance(contract_detail, dict)
            else None
        )
        claimed_version_digest = (
            version_detail.get("contract_digest")
            if isinstance(version_detail, dict)
            else None
        )
        for name, claimed in (
            ("contract_digest", claimed_contract_digest),
            ("cross_repo_versions", claimed_version_digest),
        ):
            if claimed != expected_contract_digest:
                passed.discard(name)
                failed.add(name)
                reasons.append(
                    f"required check '{name}' contract digest does not match "
                    f"checked-out manifest ({expected_contract_digest})"
                )

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
    report_checks = {name: dict(detail) for name, detail in checks.items()}
    if "contract_baseline" in passed and contract_detail is not None:
        report_checks["contract_baseline"] = {"status": "pass", "detail": contract_detail}
    return GateReport(
        environment=environment,
        scenario=scenario,
        status=status,
        passed_checks=frozenset(passed),
        skipped_checks=frozenset(skipped),
        failed_checks=frozenset(failed),
        reasons=tuple(reasons),
        checks=report_checks,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate Kernel preprod closure evidence into a pass/fail gate report."
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
        "--contract-manifest",
        default=DEFAULT_CONTRACT_MANIFEST,
        help="path to the contract baseline manifest (must have accepted=true)",
    )
    parser.add_argument(
        "--baseline-manifest",
        default=DEFAULT_BASELINE_MANIFEST,
        help="path to the frozen Agent Kernel contract manifest",
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
        merged,
        environment=args.environment,
        scenario=args.scenario,
        baseline_manifest=Path(args.baseline_manifest),
        contract_manifest=Path(args.contract_manifest),
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
