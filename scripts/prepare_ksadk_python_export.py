#!/usr/bin/env python3
"""Prepare a clean ksadk-python export candidate.

This helper is intentionally local-only. It never creates a GitHub repository,
pushes branches, enables Pages, creates releases, or publishes packages. It
exists so maintainers can review a deterministic clean-export directory before
deciding whether the public GitHub import should use rewritten history or a
fresh source snapshot.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_REPOSITORY = "https://github.com/kingsoftcloud/ksadk-python"
DOCUMENTATION_URL = "https://kingsoftcloud.github.io/ksadk-python/"

DEFAULT_OUTPUT_DIR = Path("/tmp/ksadk-python-export-candidate")

CURATED_DOCS: set[str] = {"docs/maintainer-approval-record.md"}

ROOT_EXPORT_FILES = {
    ".dockerignore",
    ".gitattributes",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/pages.yml",
    ".github/workflows/release-check.yml",
    ".github/workflows/secret-patterns.yml",
    ".gitignore",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "MANIFEST.in",
    "Makefile",
    "README.md",
    "README.en.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "mkdocs.yml",
    "pyproject.toml",
    "uv.lock",
}

EXPORT_PREFIXES = (
    "ksadk/",
    "ksadk_runtime_common/",
    "public-docs/",
)

SCRIPT_EXPORT_FILES = {
    "scripts/audit_release_artifacts.py",
    "scripts/check_approval_record.py",
    "scripts/check_publication_state.py",
    "scripts/generate_public_assets.py",
    "scripts/open_source_audit.py",
    "scripts/prepare_ksadk_python_export.py",
    "scripts/prepare_ksadk_web_export.py",
}

PUBLIC_TEST_FILES = {
    "tests/conftest.py",
    "tests/test_check_approval_record.py",
    "tests/test_check_publication_state.py",
    "tests/test_markdown_repair.py",
    "tests/test_open_source_audit.py",
    "tests/test_public_release_positioning.py",
    "tests/test_runtime_common_packaging.py",
    "tests/test_tracing_setup_otlp.py",
}

EXCLUDED_PREFIXES = (
    ".git/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".venv/",
    ".zread/",
    "__pycache__/",
    "build/",
    "dist/",
    "docs/archive/",
    "docs/internal/",
    "docs/superpowers/",
    "examples/",
    "htmlcov/",
    "ksadk.egg-info/",
    "ksadk/server/web-ui/",
    "site/",
)

EXCLUDED_PATHS = {
    ".pypirc",
    ".pypirc.example",
    "Dockerfile.docs",
    "deploy/helm/ksadk-docs/Chart.yaml",
    "deploy/helm/ksadk-docs/templates/_helpers.tpl",
    "deploy/helm/ksadk-docs/templates/deployment.yaml",
    "deploy/helm/ksadk-docs/templates/ingress.yaml",
    "deploy/helm/ksadk-docs/templates/service.yaml",
    "deploy/helm/ksadk-docs/values-online.yaml",
    "deploy/helm/ksadk-docs/values-pre.yaml",
    "deploy/helm/ksadk-docs/values.yaml",
    "scripts/zread_subpath_proxy.py",
}

EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
)


@dataclass(frozen=True)
class ExportPlan:
    ok: bool
    repo_root: str
    target_repository: str
    documentation: str
    export_paths: list[str]
    excluded_paths: list[str]
    violations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize(path: str | Path) -> str:
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def git_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "--deleted"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted_paths = {normalize(path) for path in deleted.stdout.splitlines()}
    return sorted(
        normalize(path)
        for path in completed.stdout.splitlines()
        if normalize(path) not in deleted_paths
    )


def filesystem_files(root: Path) -> list[str]:
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = normalize(path.relative_to(root))
        paths.append(rel_path)
    return paths


def discover_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        return git_files(root)
    return filesystem_files(root)


def is_excluded(path: str) -> bool:
    normalized = normalize(path)
    if not is_included_by_policy(normalized):
        return True
    if normalized in EXCLUDED_PATHS:
        return True
    if normalized.startswith("deploy/helm/ksadk-docs/"):
        return True
    if normalized.startswith("docs/") and normalized not in CURATED_DOCS:
        return True
    if normalized.endswith(EXCLUDED_SUFFIXES):
        return True
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in EXCLUDED_PREFIXES
    )


def is_included_by_policy(path: str) -> bool:
    normalized = normalize(path)
    return (
        normalized in ROOT_EXPORT_FILES
        or normalized in CURATED_DOCS
        or normalized in SCRIPT_EXPORT_FILES
        or normalized in PUBLIC_TEST_FILES
        or normalized.startswith(EXPORT_PREFIXES)
    )


def build_export_plan(repo_root: Path) -> ExportPlan:
    repo_root = repo_root.resolve()
    violations: list[str] = []
    if not repo_root.is_dir():
        violations.append(f"repo root does not exist: {repo_root}")
        discovered: list[str] = []
    else:
        try:
            discovered = discover_files(repo_root)
        except subprocess.CalledProcessError as exc:
            violations.append(f"failed to discover git files: {exc}")
            discovered = []

    export_paths = sorted(path for path in discovered if not is_excluded(path))
    excluded_paths = sorted(path for path in discovered if is_excluded(path))

    required_paths = {
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "README.md",
        "README.en.md",
        "README.zh-CN.md",
        "pyproject.toml",
        "mkdocs.yml",
    }
    for required_path in sorted(required_paths):
        if required_path not in export_paths:
            violations.append(f"missing required public file: {required_path}")

    return ExportPlan(
        ok=not violations,
        repo_root=str(repo_root),
        target_repository=TARGET_REPOSITORY,
        documentation=DOCUMENTATION_URL,
        export_paths=export_paths,
        excluded_paths=excluded_paths,
        violations=violations,
    )


def copy_export(plan: ExportPlan, output_dir: Path) -> None:
    repo_root = Path(plan.repo_root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for rel_path in plan.export_paths:
        source = repo_root / rel_path
        if not source.is_file():
            continue
        target = output_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "targetRepository": TARGET_REPOSITORY,
        "documentation": DOCUMENTATION_URL,
        "exportPathCount": len(plan.export_paths),
        "excludedPathCount": len(plan.excluded_paths),
        "excludedPaths": plan.excluded_paths,
        "includePolicy": {
            "rootFiles": sorted(ROOT_EXPORT_FILES),
            "prefixes": list(EXPORT_PREFIXES),
            "curatedDocs": sorted(CURATED_DOCS),
            "scripts": sorted(SCRIPT_EXPORT_FILES),
            "tests": sorted(PUBLIC_TEST_FILES),
        },
        "notes": [
            "Local-only clean export candidate.",
            "Clean export uses an allowlist policy for the first public GitHub snapshot.",
            "Run public-repo audit before importing to GitHub.",
            "Do not include PyPI/TestPyPI credentials, .pypirc files, or CI secrets.",
        ],
    }
    (output_dir / "export-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def format_summary(plan: ExportPlan) -> list[str]:
    return [
        f"ok: {str(plan.ok).lower()}",
        f"target repository: {plan.target_repository}",
        f"documentation: {plan.documentation}",
        f"export paths: {len(plan.export_paths)}",
        f"excluded paths: {len(plan.excluded_paths)}",
    ]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--summary", action="store_true", help="print a concise human-readable summary")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    plan = build_export_plan(args.repo_root)

    if plan.ok and args.output_dir:
        copy_export(plan, args.output_dir)

    payload = plan.to_dict()
    if args.output_dir:
        payload["outputDir"] = str(args.output_dir.resolve())

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(format_summary(plan)))
        for violation in plan.violations:
            print(f"violation: {violation}", file=sys.stderr)

    return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
