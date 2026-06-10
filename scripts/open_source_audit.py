#!/usr/bin/env python3
"""Audit public release file lists against open-source denylist rules."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class DenyRule:
    name: str
    prefixes: tuple[str, ...]
    contains: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    description: str = ""

    def matches(self, path: str) -> bool:
        normalized = normalize_path(path)
        if normalized in self.allowed_paths:
            return False
        return (
            normalized.startswith(self.prefixes)
            or any(f"/{prefix}" in normalized for prefix in self.prefixes)
            or any(part in normalized for part in self.contains)
        )


@dataclass(frozen=True)
class Violation:
    path: str
    rule: str
    description: str


@dataclass(frozen=True)
class ContentRule:
    name: str
    pattern: re.Pattern[str]
    description: str

    def matches(self, text: str) -> bool:
        return self.pattern.search(text) is not None


@dataclass(frozen=True)
class AuditResult:
    target: str
    ok: bool
    counts: dict[str, int]
    violations: list[Violation]

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "ok": self.ok,
            "counts": self.counts,
            "violations": [asdict(violation) for violation in self.violations],
        }


COMMON_RULES = (
    DenyRule(
        name="zread-output",
        prefixes=(".zread/",),
        description=".zread output is an internal/code-understanding snapshot, not a public artifact",
    ),
    DenyRule(
        name="internal-docs",
        prefixes=("docs/internal/",),
        description="internal docs must be removed or migrated before publication",
    ),
    DenyRule(
        name="node-modules",
        prefixes=("node_modules/",),
        contains=("/node_modules/",),
        description="vendored node_modules must not be published",
    ),
    DenyRule(
        name="pypirc-file",
        prefixes=(".pypirc", "pypirc"),
        contains=("/.pypirc", "/pypirc"),
        description="PyPI upload credentials must stay outside the public repository",
    ),
    DenyRule(
        name="helm-values",
        prefixes=("deploy/helm/",),
        contains=("/deploy/helm/",),
        description="helm deployment values may contain internal routing or registry details",
    ),
)

PUBLIC_REPO_RULES = COMMON_RULES + (
    DenyRule(
        name="non-curated-docs",
        prefixes=("docs/",),
        allowed_paths=("docs/maintainer-approval-record.md",),
        description="internal planning and technical design docs stay out of the public repository; user docs live in public-docs/",
    ),
    DenyRule(
        name="internal-deploy-material",
        prefixes=(
            "deploy/",
            "Makefile.openclaw",
            "Makefile.promo.Dockerfile",
        ),
        description="internal deployment shells, runtime images, and private registry examples stay out of the first public repository snapshot",
    ),
    DenyRule(
        name="internal-agent-ops-material",
        prefixes=(
            "skills/agentengine-",
        ),
        description="internal agent/operator playbooks can expose private operations, kubeconfig paths, or support procedures",
    ),
    DenyRule(
        name="non-curated-examples",
        prefixes=("examples/",),
        description="examples must be separately curated and scrubbed before first public publication",
    ),
    DenyRule(
        name="env-example",
        prefixes=(".env.example",),
        contains=("/.env.example",),
        description="environment examples must be curated before publication to avoid private endpoints or credential names",
    ),
)

WHEEL_RULES = (
    DenyRule(
        name="hosted-ui-bundle",
        prefixes=("ksadk/server/web-ui/dist-hosted/",),
        description="hosted UI production bundle should not be part of the SDK wheel by default",
    ),
    DenyRule(
        name="web-ui-source",
        prefixes=("ksadk/server/web-ui/",),
        description="editable frontend source and Web UI build inputs should not be part of the SDK package",
    ),
)

GITHUB_PAGES_RULES = (
    DenyRule(
        name="zread-site",
        prefixes=(".zread/site/",),
        description="GitHub Pages must use curated public docs, not raw zread generated pages",
    ),
)

KSADK_WEB_CANDIDATE_RULES = COMMON_RULES + (
    DenyRule(
        name="hosted-deployment-shell",
        prefixes=(
            ".dockerignore",
            "Dockerfile",
            "nginx.conf",
            "deploy/",
            "dist/",
            "dist-hosted/",
            "node_modules/",
            "scripts/sync-static.mjs",
            "tsconfig.tsbuildinfo",
        ),
        description="KSADK Web candidate must not include hosted deployment shells, generated bundles, or consumer sync scripts",
    ),
    DenyRule(
        name="hosted-only-tests",
        prefixes=("tests/helm-contract.test.mjs", "tests/makefile-contract.test.mjs"),
        description="hosted-only deployment contract tests stay in the hosted UI repository",
    ),
    DenyRule(
        name="ksadk-only-tests",
        prefixes=("tests/hosted-ui-sync.test.mjs", "tests/sync-static.test.mjs"),
        description="KSADK-only consumer tests stay in ksadk-python",
    ),
)

TARGET_RULES: dict[str, tuple[DenyRule, ...]] = {
    "public-repo": PUBLIC_REPO_RULES,
    "sdist": COMMON_RULES + WHEEL_RULES,
    "wheel": COMMON_RULES + WHEEL_RULES,
    "github-pages": COMMON_RULES + GITHUB_PAGES_RULES,
    "ksadk-web-candidate": KSADK_WEB_CANDIDATE_RULES,
}

CONTENT_AUDIT_TARGETS = {"public-repo", "ksadk-web-candidate", "sdist", "wheel"}

CONTENT_RULES = (
    ContentRule(
        name="private-doc-domain",
        pattern=re.compile(r"https?://(?:ksadk\.kingsoft\.com/docs|private-docs\.example\.invalid)"),
        description="public docs and package metadata should point to GitHub Pages",
    ),
    ContentRule(
        name="local-absolute-path",
        pattern=re.compile(r"/Users/[A-Za-z0-9._-]+/(?:kingsoft|agentengine-test|Downloads)\b"),
        description="local workstation paths must not be published",
    ),
    ContentRule(
        name="internal-git-remote",
        pattern=re.compile(r"(?:ssh://)?ezone\.ksyun\.com(?::\d+)?/"),
        description="internal Git remote URLs must stay out of public artifacts",
    ),
    ContentRule(
        name="internal-service-endpoint",
        pattern=re.compile(
            r"(?<![A-Za-z0-9.-])"
            r"(?!(?:aicp)\.(?:inner|internal)\.api\.ksyun\.com\b)"
            r"(?:[A-Za-z0-9-]+\.)*(?:inner\.api|internal\.api|sdns)\.ksyun\.com\b"
        ),
        description="internal service endpoints must not be published unless explicitly supported by the public SDK",
    ),
    ContentRule(
        name="private-container-registry",
        pattern=re.compile(r"\bhub(?:-[A-Za-z0-9-]+)?\.kce\.ksyun\.com\b"),
        description="private container registry defaults must not be published",
    ),
    ContentRule(
        name="aws-access-key-id",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        description="AWS-style access key IDs must not be published",
    ),
    ContentRule(
        name="openai-api-key",
        pattern=re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"),
        description="OpenAI-style API keys must not be published",
    ),
    ContentRule(
        name="github-token",
        pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,}\b"),
        description="GitHub tokens must not be published",
    ),
    ContentRule(
        name="long-lived-secret-assignment",
        pattern=re.compile(
            r"(?i)\b(?:secret_key|access_key|api_key|token|password)\b\s*[:=]\s*"
            r"[\"']?(?!"
            r"dummy|test|fake|example|placeholder|"
            r"sk-test|sk-live|secret-token|secret-key|super-secret|skill-service|"
            r"gateway-token-demo|stale-secret|cli-app-secret|secret-demo|"
            r"my-secret-token|kdocs-test-token|mem0-secret"
            r")"
            r"(?:[A-Za-z0-9_./+=-]{24,})(?:[\"']|\s*$)"
        ),
        description="Long-lived secret-looking assignments must not be published",
    ),
    ContentRule(
        name="private-key-material",
        pattern=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        description="private key material must not be published",
    ),
)

TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

TEXT_FILENAMES = {
    ".dockerignore",
    ".gitignore",
    "Dockerfile",
    "LICENSE",
    "Makefile",
}


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def audit_paths(target: str, paths: Iterable[str]) -> AuditResult:
    if target not in TARGET_RULES:
        known = ", ".join(sorted(TARGET_RULES))
        raise ValueError(f"unknown target {target!r}; expected one of: {known}")

    checked = [normalize_path(path) for path in paths if normalize_path(path)]
    violations: list[Violation] = []
    for path in checked:
        for rule in TARGET_RULES[target]:
            if rule.matches(path):
                violations.append(
                    Violation(path=path, rule=rule.name, description=rule.description)
                )
                break

    return AuditResult(
        target=target,
        ok=not violations,
        counts={"checked": len(checked), "violations": len(violations)},
        violations=violations,
    )


def should_scan_text(path: str) -> bool:
    normalized = normalize_path(path)
    file_name = Path(normalized).name
    suffix = Path(normalized).suffix.lower()
    return file_name in TEXT_FILENAMES or suffix in TEXT_SUFFIXES


def audit_file_contents(root: Path, paths: Iterable[str]) -> AuditResult:
    checked = 0
    violations: list[Violation] = []
    for raw_path in paths:
        normalized = normalize_path(raw_path)
        if not normalized or not should_scan_text(normalized):
            continue

        path = root / normalized
        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        checked += 1
        for rule in CONTENT_RULES:
            if rule.matches(text):
                violations.append(
                    Violation(path=normalized, rule=rule.name, description=rule.description)
                )
                break

    return AuditResult(
        target="content",
        ok=not violations,
        counts={"checked": checked, "violations": len(violations)},
        violations=violations,
    )


def audit_ksadk_web_candidate_metadata(root: Path, paths: Iterable[str]) -> AuditResult:
    path_set = {normalize_path(path) for path in paths}
    violations: list[Violation] = []

    required_paths = {
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/pull_request_template.md",
        ".github/workflows/ci.yml",
        ".github/workflows/pages.yml",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "export-manifest.json",
        "package-lock.json",
        "package.json",
    }
    for required_path in sorted(required_paths.difference(path_set)):
        violations.append(
            Violation(
                path=required_path,
                rule="missing-ksadk-web-file",
                description="KSADK Web candidate is missing required public repository scaffolding",
            )
        )

    package_path = root / "package.json"
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            violations.append(
                Violation(
                    path="package.json",
                    rule="invalid-json",
                    description="package.json must be valid JSON",
                )
            )
        else:
            expected_scripts = {
                "build": "npm run build:ksadk",
                "build:ksadk": "VITE_BASE_PATH=./ vite build --outDir dist-ksadk",
                "build:hosted": "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted",
            }
            if package.get("name") != "@kingsoftcloud/ksadk-web":
                violations.append(
                    Violation(
                        path="package.json",
                        rule="wrong-ksadk-web-package-name",
                        description="KSADK Web package must use the public scoped package name",
                    )
                )
            if package.get("private") is not None:
                violations.append(
                    Violation(
                        path="package.json",
                        rule="private-package",
                        description="KSADK Web candidate package.json must not be private",
                    )
                )
            if package.get("license") != "Apache-2.0":
                violations.append(
                    Violation(
                        path="package.json",
                        rule="wrong-ksadk-web-license",
                        description="KSADK Web candidate must use Apache-2.0",
                    )
                )
            if package.get("homepage") != "https://kingsoftcloud.github.io/ksadk-web/":
                violations.append(
                    Violation(
                        path="package.json",
                        rule="wrong-ksadk-web-homepage",
                        description="KSADK Web homepage must point to its public GitHub Pages demo/docs URL",
                    )
                )
            scripts = package.get("scripts", {})
            if not isinstance(scripts, dict):
                scripts = {}
            for script_name, expected in expected_scripts.items():
                if scripts.get(script_name) != expected:
                    violations.append(
                        Violation(
                            path="package.json",
                            rule="wrong-ksadk-web-build-script",
                            description=f"{script_name} must be {expected!r}",
                        )
                    )
            if "scripts/sync-static.mjs" in json.dumps(package, ensure_ascii=False):
                violations.append(
                    Violation(
                        path="package.json",
                        rule="consumer-sync-script-reference",
                        description="KSADK Web package scripts must not call KSADK consumer sync scripts",
                    )
                )

    manifest_path = root / "export-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            violations.append(
                Violation(
                    path="export-manifest.json",
                    rule="invalid-json",
                    description="export manifest must be valid JSON",
                )
            )
        else:
            if manifest.get("targetRepository") != "https://github.com/kingsoftcloud/ksadk-web":
                violations.append(
                    Violation(
                        path="export-manifest.json",
                        rule="wrong-ksadk-web-target-repository",
                        description="export manifest must point to the public KSADK Web repository",
                    )
                )
            if manifest.get("publicDemo") != "https://kingsoftcloud.github.io/ksadk-web/":
                violations.append(
                    Violation(
                        path="export-manifest.json",
                        rule="wrong-ksadk-web-public-demo",
                        description="export manifest must record the public KSADK Web GitHub Pages URL",
                    )
                )
            if not manifest.get("generatedCandidateFiles"):
                violations.append(
                    Violation(
                        path="export-manifest.json",
                        rule="missing-generated-candidate-files",
                        description="export manifest must record generated governance files",
                    )
                )

    return AuditResult(
        target="ksadk-web-candidate-metadata",
        ok=not violations,
        counts={"checked": len(required_paths), "violations": len(violations)},
        violations=violations,
    )


def merge_results(target: str, results: Sequence[AuditResult]) -> AuditResult:
    violations = [violation for result in results for violation in result.violations]
    return AuditResult(
        target=target,
        ok=not violations,
        counts={
            "checked": sum(result.counts["checked"] for result in results),
            "violations": len(violations),
        },
        violations=violations,
    )


def git_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted = subprocess.run(
        ["git", "ls-files", "--deleted"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    deleted_paths = {normalize_path(path) for path in deleted.stdout.splitlines()}
    return sorted(
        path for path in completed.stdout.splitlines() if normalize_path(path) not in deleted_paths
    )


def filesystem_files(root: Path) -> list[str]:
    ignored_dirs = {".git", "__pycache__"}
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in ignored_dirs for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            paths.append(normalize_path(str(path.relative_to(root))))
    return paths


def discover_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        return git_files(root)
    return filesystem_files(root)


def file_list_from_path(path: Path) -> list[str]:
    if path == Path("-"):
        return sys.stdin.read().splitlines()
    return path.read_text(encoding="utf-8").splitlines()


def render_text(result: AuditResult) -> str:
    lines = [
        f"target: {result.target}",
        f"checked: {result.counts['checked']}",
        f"violations: {result.counts['violations']}",
    ]
    for violation in result.violations:
        lines.append(f"- {violation.path}: {violation.rule} ({violation.description})")
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=sorted(TARGET_RULES),
        default="public-repo",
        help="artifact type whose denylist rules should be applied",
    )
    parser.add_argument(
        "--file-list",
        type=Path,
        help="newline-delimited artifact file list; use '-' to read stdin",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root used when --file-list is omitted",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = file_list_from_path(args.file_list) if args.file_list else discover_files(args.root)
    result = audit_paths(args.target, paths)
    if args.target == "ksadk-web-candidate" or (
        args.target in CONTENT_AUDIT_TARGETS and not args.file_list
    ):
        result = merge_results(
            args.target,
            [
                result,
                audit_file_contents(args.root, paths),
                *(
                    [audit_ksadk_web_candidate_metadata(args.root, paths)]
                    if args.target == "ksadk-web-candidate"
                    else []
                ),
            ],
        )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
