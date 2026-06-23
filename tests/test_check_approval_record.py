from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_approval_record.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_approval_record", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _approved_record(python_source: str = "cd5fa22b1e78f03a8a9d025017e97ad414fdaa74") -> str:
    return """# ksadk Public Release Approval Record

## Required Approval Decisions

| Decision | Approved value |
| --- | --- |
| License | Apache-2.0 |
| Python repository | kingsoftcloud/ksadk-python |
| Web UI repository | kingsoftcloud/ksadk-web |
| Python package version | 0.6.6 |
| Public docs URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Package metadata repository URL | https://github.com/kingsoftcloud/ksadk-python |
| Package metadata documentation URL | https://kingsoftcloud.github.io/ksadk-python/ |
| Security contact | security@kingsoft.com |

## Publication Strategy

| Strategy | Approved |
| --- | --- |
| Reviewed GitHub pull request | No |
| Clean export from reviewed candidate | Yes |
| Rewritten Git history after secret scan | No |

The approved strategy must name the commit, tag, or export archive used for:

- `ksadk-python`: {python_source}
- `ksadk-web`: /tmp/ksadk-web-export-candidate

## Approval Sign-Off

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| Maintainer | Alice | Approved | 2026-05-28 |
| Security reviewer | Bob | Approved | 2026-05-28 |
| Release owner | Carol | Approved | 2026-05-28 |
""".format(python_source=python_source)


def test_template_approval_record_fails_until_strategy_and_signoffs_are_filled():
    module = _load_module()

    checks = module.validate_approval_record(
        REPO_ROOT / "docs" / "maintainer-approval-record.md",
        version="0.6.6",
        expected_current_commit="current-reviewed-commit",
    )

    failed = {check.name for check in checks if not check.ok}
    assert "publication-strategy:single-approved" in failed
    assert "publication-strategy:ksadk-python-source" in failed
    assert "publication-strategy:ksadk-web-source" in failed
    assert "publication-strategy:ksadk-python-current-commit" in failed
    assert "publication-strategy:ksadk-web-current-commit" in failed
    assert "signoff:Maintainer" in failed
    assert "signoff:Security reviewer" in failed
    assert "signoff:Release owner" in failed
    assert all(check.ok for check in checks if check.name.startswith("decision:"))


def test_filled_approval_record_passes(tmp_path):
    module = _load_module()
    record = tmp_path / "approval.md"
    record.write_text(_approved_record(), encoding="utf-8")

    checks = module.validate_approval_record(record, version="0.6.6", expected_current_commit="")

    assert all(check.ok for check in checks)


def test_filled_record_fails_when_source_references_do_not_match_current_commit(tmp_path):
    module = _load_module()
    record = tmp_path / "approval.md"
    record.write_text(_approved_record("old-reviewed-source"), encoding="utf-8")

    checks = module.validate_approval_record(
        record,
        version="0.6.6",
        expected_current_commit="new-reviewed-commit",
    )

    failed = {check.name for check in checks if not check.ok}
    assert "publication-strategy:ksadk-python-current-commit" in failed
    assert "publication-strategy:ksadk-web-current-commit" in failed


def test_filled_record_passes_when_source_references_include_current_commit(tmp_path):
    module = _load_module()
    record = tmp_path / "approval.md"
    record.write_text(
        _approved_record("reviewed export from new-reviewed-commit")
        .replace(
            "- `ksadk-web`: /tmp/ksadk-web-export-candidate",
            "- `ksadk-web`: /tmp/ksadk-web-export-candidate at new-reviewed-commit",
        ),
        encoding="utf-8",
    )

    checks = module.validate_approval_record(
        record,
        version="0.6.6",
        expected_current_commit="new-reviewed-commit",
    )

    assert all(check.ok for check in checks)


def test_cli_json_reports_failed_template_record():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any(
        check["name"] == "publication-strategy:single-approved" and not check["ok"]
        for check in payload["checks"]
    )
