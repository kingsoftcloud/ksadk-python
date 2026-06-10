#!/usr/bin/env python3
"""Validate the public release approval record before external writes.

The approval record is intentionally local release evidence. It should be
provided before running commands that write to GitHub Releases, TestPyPI, or
PyPI. The checker only validates public-safe decisions and avoids embedding
private repository URLs or internal review-channel names.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APPROVAL_RECORD = REPO_ROOT / "docs" / "maintainer-approval-record.md"
REQUIRED_SIGNOFF_ROLES = ("Maintainer", "Security reviewer", "Release owner")
STRATEGIES = (
    "Reviewed GitHub pull request",
    "Clean export from reviewed candidate",
    "Rewritten Git history after secret scan",
)


@dataclass(frozen=True)
class ApprovalCheck:
    name: str
    ok: bool
    detail: str


def _current_version() -> str:
    pyproject = REPO_ROOT / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("pyproject.toml 中未找到 version")


def _expected_decisions(version: str) -> dict[str, str]:
    return {
        "License": "Apache-2.0",
        "Python repository": "kingsoftcloud/ksadk-python",
        "Web UI repository": "kingsoftcloud/ksadk-web",
        "Python package version": version,
        "Public docs URL": "https://kingsoftcloud.github.io/ksadk-python/",
        "Package metadata repository URL": "https://github.com/kingsoftcloud/ksadk-python",
        "Package metadata documentation URL": "https://kingsoftcloud.github.io/ksadk-python/",
        "Security contact": "security@kingsoft.com",
    }


def _table_rows(text: str, section: str) -> list[list[str]]:
    match = re.search(rf"^## {re.escape(section)}\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        return []
    rows: list[list[str]] = []
    for line in match.group("body").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or all(set(cell) <= {"-"} for cell in cells):
            continue
        if cells[0].lower() in {"decision", "strategy", "role"}:
            continue
        rows.append(cells)
    return rows


def _clean_cell(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1].strip()
    return value


def _decision_map(text: str) -> dict[str, str]:
    return {
        _clean_cell(cells[0]): _clean_cell(cells[1])
        for cells in _table_rows(text, "Required Approval Decisions")
        if len(cells) >= 2
    }


def _strategy_map(text: str) -> dict[str, str]:
    return {
        cells[0]: cells[1]
        for cells in _table_rows(text, "Publication Strategy")
        if len(cells) >= 2
    }


def _signoff_rows(text: str) -> dict[str, list[str]]:
    return {
        cells[0]: cells
        for cells in _table_rows(text, "Approval Sign-Off")
        if len(cells) >= 4
    }


def _source_ref(text: str, name: str) -> str:
    match = re.search(rf"^-\s*`{re.escape(name)}`\s*:\s*(?P<value>.+?)\s*$", text, re.M)
    return _clean_cell(match.group("value")) if match else ""


def _current_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return completed.stdout.strip()


def _source_ref_is_filled(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(normalized) and normalized not in {
        "tbd",
        "todo",
        "no",
        "none",
        "n/a",
        "<reviewed source reference>",
    }


def validate_approval_record(
    path: Path,
    *,
    version: str,
    expected_current_commit: str = "",
) -> list[ApprovalCheck]:
    if not path.is_file():
        return [ApprovalCheck("approval-record:file", False, f"missing file: {path}")]
    text = path.read_text(encoding="utf-8")
    checks: list[ApprovalCheck] = []

    decisions = _decision_map(text)
    for decision, expected in _expected_decisions(version).items():
        actual = decisions.get(decision, "")
        checks.append(
            ApprovalCheck(
                name=f"decision:{decision}",
                ok=actual == expected,
                detail=json.dumps({"actual": actual, "expected": expected}, ensure_ascii=False),
            )
        )

    strategies = _strategy_map(text)
    approved = [name for name in STRATEGIES if strategies.get(name, "").lower() == "yes"]
    checks.append(
        ApprovalCheck(
            name="publication-strategy:single-approved",
            ok=len(approved) == 1,
            detail=json.dumps(
                {"approved": approved, "expected": "exactly one reviewed publication strategy"},
                ensure_ascii=False,
            ),
        )
    )

    python_source_ref = _source_ref(text, "ksadk-python")
    web_source_ref = _source_ref(text, "ksadk-web")
    source_refs = {
        "ksadk-python": python_source_ref,
        "ksadk-web": web_source_ref,
    }
    for source_name, source_ref in source_refs.items():
        checks.append(
            ApprovalCheck(
                name=f"publication-strategy:{source_name}-source",
                ok=_source_ref_is_filled(source_ref),
                detail=json.dumps(
                    {
                        "actual": source_ref,
                        "expected": "approved source reference",
                    },
                    ensure_ascii=False,
                ),
            )
        )
        if expected_current_commit:
            checks.append(
                ApprovalCheck(
                    name=f"publication-strategy:{source_name}-current-commit",
                    ok=expected_current_commit in source_ref,
                    detail=json.dumps(
                        {
                            "actual": source_ref,
                            "expectedCommit": expected_current_commit,
                        },
                        ensure_ascii=False,
                    ),
                )
            )

    signoffs = _signoff_rows(text)
    for role in REQUIRED_SIGNOFF_ROLES:
        cells = signoffs.get(role, [])
        filled = len(cells) >= 4 and all(cell.strip() for cell in cells[1:4])
        checks.append(
            ApprovalCheck(
                name=f"signoff:{role}",
                ok=filled,
                detail="name, decision, and date must be filled",
            )
        )

    return checks


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-record", type=Path, default=DEFAULT_APPROVAL_RECORD)
    parser.add_argument("--version", default=_current_version())
    parser.add_argument(
        "--expected-current-commit",
        default=None,
        help="commit SHA that approved source references must include; defaults to git rev-parse HEAD",
    )
    parser.add_argument("--json", action="store_true", help="print JSON output")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    expected_current_commit = (
        _current_commit() if args.expected_current_commit is None else args.expected_current_commit
    )
    checks = validate_approval_record(
        args.approval_record,
        version=args.version,
        expected_current_commit=expected_current_commit,
    )
    ok = all(check.ok for check in checks)
    payload = {
        "ok": ok,
        "approvalRecord": str(args.approval_record),
        "checks": [asdict(check) for check in checks],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"approval record: {args.approval_record}")
        for check in checks:
            state = "ok" if check.ok else "fail"
            print(f"{state}: {check.name} - {check.detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
