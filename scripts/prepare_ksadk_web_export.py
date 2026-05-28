#!/usr/bin/env python3
"""Prepare a clean ksadk-web export candidate.

This helper is intentionally local-only. It never creates a GitHub repository,
pushes branches, or publishes artifacts. With no --output-dir it only verifies
the export boundary and prints a summary.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOSTED_ROOT = REPO_ROOT.parents[2] / "agentengine-hosted-ui"
DEFAULT_KSADK_WEB_UI = REPO_ROOT / "ksadk" / "server" / "web-ui"

ROOT_EXPORT_FILES = (
    "components.json",
    "eslint.config.js",
    "index.html",
    "package-lock.json",
    "package.json",
    "postcss.config.js",
    "tailwind.config.ts",
    "tsconfig.app.json",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
)

SHARED_DIRECTORIES = ("public", "src")

COMMON_TESTS = (
    "capabilities.test.mjs",
    "feedback-utils.test.mjs",
    "mobile-layout.test.mjs",
    "native-platform.test.mjs",
    "responses-stream.test.mjs",
    "run-state.test.mjs",
    "session-events.test.mjs",
    "session-list.test.mjs",
    "session-persistence.test.mjs",
    "sidebar-contract.test.mjs",
    "stream-control.test.mjs",
    "terminal-session.test.mjs",
    "tool-display.test.mjs",
    "ui-utils.test.mjs",
    "workspace-panel-contract.test.mjs",
    "workspace-utils.test.mjs",
)

HOSTED_ONLY_TESTS = ("helm-contract.test.mjs", "makefile-contract.test.mjs")
KSADK_ONLY_TESTS = ("hosted-ui-sync.test.mjs", "sync-static.test.mjs")

FORBIDDEN_PREFIXES = (
    ".git/",
    "deploy/",
    "dist/",
    "dist-hosted/",
    "node_modules/",
)

FORBIDDEN_FILES = {
    ".dockerignore",
    "Dockerfile",
    "Makefile",
    "nginx.conf",
    "sandbox-poc.html",
    "scripts/sync-static.mjs",
    "tsconfig.tsbuildinfo",
    *(f"tests/{name}" for name in HOSTED_ONLY_TESTS),
    *(f"tests/{name}" for name in KSADK_ONLY_TESTS),
}

KSADK_WEB_PACKAGE_NAME = "@kingsoftcloud/ksadk-web"
KSADK_WEB_REPOSITORY = "https://github.com/kingsoftcloud/ksadk-web"
KSADK_WEB_PAGES_URL = "https://kingsoftcloud.github.io/ksadk-web/"

GENERATED_CANDIDATE_FILES = {
    ".github/ISSUE_TEMPLATE/bug_report.md": """---
name: Bug report
about: Report a reproducible problem in the public KSADK Web UI
title: "[Bug]: "
labels: bug
assignees: ""
---

## Summary

Describe the problem and expected behavior.

## Environment

- KSADK Web ref:
- Node version:
- Browser:
- Consumer: hosted UI / KSADK static UI / local development

## Reproduction

```bash
# Minimal commands or script
```

## Logs or Screenshots

Do not include tokens, credentials, private URLs, or customer data.
""",
    ".github/ISSUE_TEMPLATE/feature_request.md": """---
name: Feature request
about: Suggest a public UI, integration, or documentation improvement
title: "[Feature]: "
labels: enhancement
assignees: ""
---

## Problem

What user workflow or integration is blocked?

## Proposal

Describe the desired behavior.

## Consumer Impact

Mention whether this affects hosted UI, KSADK static UI, or both.
""",
    ".github/pull_request_template.md": """## Summary

-

## Validation

- [ ] `npm ci`
- [ ] `npm test`
- [ ] `npm run build:ksadk`
- [ ] `npm run build:hosted`

## Public Surface

- [ ] README or public docs updated if user-facing behavior changed.
- [ ] Consumer impact for hosted UI and KSADK static UI is documented.

## Security

- [ ] No tokens, credentials, private URLs, customer data, internal deployment notes, or generated deployment bundles are introduced.
- [ ] Maintainer review is required before public release or publish actions.
""",
    ".github/workflows/ci.yml": """name: CI

on:
  pull_request:
  push:
    branches:
      - main
      - master
      - "open-source/**"

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm

      - run: npm ci
      - run: npm test
      - run: npm run build:ksadk
      - run: npm run build:hosted
""",
    ".github/workflows/pages.yml": """name: Pages

on:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm

      - run: npm ci
      - run: npm test
      - run: npm run build:ksadk

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: dist-ksadk

  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    needs: build
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
""",
    ".gitignore": """node_modules/
dist/
dist-hosted/
dist-ksadk/
coverage/
*.tsbuildinfo
.DS_Store
""",
    "CONTRIBUTING.md": """# Contributing

Thanks for helping improve KSADK Web.

## Development Setup

```bash
git clone https://github.com/kingsoftcloud/ksadk-web.git
cd ksadk-web
npm ci
```

## Local Checks

```bash
npm test
npm run build:ksadk
npm run build:hosted
```

Keep hosted deployment shell files, Dockerfiles, Helm charts, private endpoints,
customer screenshots, and generated bundles out of this repository. Consumer
repositories should reference a reviewed KSADK Web tag or commit.

Do not push, publish, or create a release before maintainer review.
""",
    "README.md": """# KSADK Web

KSADK Web is the shared Web UI source for AgentEngine hosted UI and
the KSADK embedded static UI.

## Consumers

- `kingsoftcloud/ksadk-python` consumes the `build:ksadk` output for
  `ksadk/server/static`.
- `agentengine-hosted-ui` consumes the `build:hosted` output and keeps private
  deployment shell files such as Docker, nginx, Helm, image tags, and runtime
  environment injection outside this repository.

## Public Demo

The GitHub Pages demo is published from the reviewed `build:ksadk` output:

https://kingsoftcloud.github.io/ksadk-web/

## Development

```bash
npm ci
npm run dev
npm test
```

## Builds

```bash
npm run build:ksadk
npm run build:hosted
npm run build:all
```

`build:ksadk` uses relative assets for the SDK embedded UI. `build:hosted`
uses the `/chat/` base path for the hosted UI bundle.

## Release Contract

Consumers should record the KSADK Web tag or commit they build from. KSADK
release notes must mention the KSADK Web ref used to generate
`ksadk/server/static`.
""",
    "SECURITY.md": """# Security Policy

## Reporting a Vulnerability

Please do not report security vulnerabilities in public issues.

Send reports to `security@kingsoft.com` with:

- Affected version, commit, or build artifact.
- Reproduction steps and expected impact.
- Any proof-of-concept code, logs, or screenshots that are safe to share.
- Whether the report may involve credentials, private endpoints, or customer
  data.

## Scope

In scope:

- Public KSADK Web source.
- Public build scripts for hosted and KSADK static UI bundles.
- Public examples and documentation.

Out of scope for this repository:

- Hosted production Docker, nginx, Helm, image registry, gateway, and runtime
  environment injection details.
- Internal AgentEngine control-plane services.
- Credentials, tokens, or customer data discovered outside the public
  repository.
""",
}


@dataclass(frozen=True)
class ExportPlan:
    ok: bool
    hosted_root: str
    ksadk_web_ui: str
    export_paths: list[str]
    hosted_only_tests: list[str]
    ksadk_only_tests: list[str]
    forbidden_paths_seen: list[str]
    violations: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize(path: Path | str) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def iter_files(root: Path, prefix: str = "") -> Iterable[str]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = normalize(path.relative_to(root))
            yield f"{prefix}{rel}" if prefix else rel


def is_forbidden(path: str) -> bool:
    return path in FORBIDDEN_FILES or any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)


def compare_file(hosted_root: Path, ksadk_web_ui: Path, rel_path: str) -> str | None:
    hosted_file = hosted_root / rel_path
    ksadk_file = ksadk_web_ui / rel_path
    if not hosted_file.is_file():
        return f"missing hosted file: {rel_path}"
    if not ksadk_file.is_file():
        return f"missing ksadk file: {rel_path}"
    if not filecmp.cmp(hosted_file, ksadk_file, shallow=False):
        return f"shared file differs: {rel_path}"
    return None


def collect_export_paths(hosted_root: Path) -> list[str]:
    paths: list[str] = []
    paths.extend(path for path in ROOT_EXPORT_FILES if (hosted_root / path).is_file())

    for directory in SHARED_DIRECTORIES:
        paths.extend(iter_files(hosted_root / directory, prefix=f"{directory}/"))

    for test_name in COMMON_TESTS:
        rel_path = f"tests/{test_name}"
        if (hosted_root / rel_path).is_file():
            paths.append(rel_path)

    return sorted(set(paths))


def collect_forbidden_paths(root: Path) -> list[str]:
    paths: set[str] = set()
    for prefix in FORBIDDEN_PREFIXES:
        candidate = root / prefix.rstrip("/")
        if candidate.exists():
            paths.add(prefix)

    for file_name in FORBIDDEN_FILES:
        candidate = root / file_name
        if candidate.exists():
            paths.add(file_name)

    return sorted(paths)


def candidate_package_json(source: dict[str, object]) -> dict[str, object]:
    package = dict(source)
    scripts = dict(package.get("scripts", {}))
    scripts.update(
        {
            "build": "npm run build:ksadk",
            "build:ksadk": "VITE_BASE_PATH=./ vite build --outDir dist-ksadk",
            "build:hosted": "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted",
            "build:all": "npm run build:ksadk && npm run build:hosted",
        }
    )
    scripts.pop("sync-static", None)

    package.update(
        {
            "name": KSADK_WEB_PACKAGE_NAME,
            "version": "0.1.0",
            "description": "Shared Web UI source for AgentEngine hosted UI and KSADK static UI.",
            "license": "Apache-2.0",
            "repository": {"type": "git", "url": f"git+{KSADK_WEB_REPOSITORY}.git"},
            "bugs": {"url": f"{KSADK_WEB_REPOSITORY}/issues"},
            "homepage": KSADK_WEB_PAGES_URL,
            "scripts": scripts,
        }
    )
    package.pop("private", None)
    return package


def normalize_package_files(output_dir: Path) -> None:
    package_path = output_dir / "package.json"
    if package_path.is_file():
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package_path.write_text(
            json.dumps(candidate_package_json(package), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    lock_path = output_dir / "package-lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["name"] = KSADK_WEB_PACKAGE_NAME
        lock["version"] = "0.1.0"
        root_package = lock.get("packages", {}).get("")
        if isinstance(root_package, dict):
            root_package["name"] = KSADK_WEB_PACKAGE_NAME
            root_package["version"] = "0.1.0"
        lock_path.write_text(
            json.dumps(lock, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def write_candidate_governance(output_dir: Path) -> None:
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    (output_dir / "LICENSE").write_text(license_text, encoding="utf-8")
    for rel_path, content in GENERATED_CANDIDATE_FILES.items():
        path = output_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_export_plan(hosted_root: Path, ksadk_web_ui: Path) -> ExportPlan:
    hosted_root = hosted_root.resolve()
    ksadk_web_ui = ksadk_web_ui.resolve()
    export_paths = collect_export_paths(hosted_root)

    violations: list[str] = []
    if not hosted_root.is_dir():
        violations.append(f"hosted root does not exist: {hosted_root}")
    if not ksadk_web_ui.is_dir():
        violations.append(f"ksadk web-ui root does not exist: {ksadk_web_ui}")

    for rel_path in export_paths:
        violation = compare_file(hosted_root, ksadk_web_ui, rel_path)
        if violation:
            violations.append(violation)

    expected_common_tests = {f"tests/{name}" for name in COMMON_TESTS}
    missing_common_tests = sorted(expected_common_tests.difference(export_paths))
    violations.extend(f"missing common test: {path}" for path in missing_common_tests)

    forbidden_in_export = sorted(path for path in export_paths if is_forbidden(path))
    violations.extend(f"forbidden path selected for export: {path}" for path in forbidden_in_export)

    return ExportPlan(
        ok=not violations,
        hosted_root=str(hosted_root),
        ksadk_web_ui=str(ksadk_web_ui),
        export_paths=export_paths,
        hosted_only_tests=[name for name in HOSTED_ONLY_TESTS if (hosted_root / "tests" / name).is_file()],
        ksadk_only_tests=[name for name in KSADK_ONLY_TESTS if (ksadk_web_ui / "tests" / name).is_file()],
        forbidden_paths_seen=collect_forbidden_paths(hosted_root),
        violations=violations,
    )


def copy_export(plan: ExportPlan, output_dir: Path) -> None:
    hosted_root = Path(plan.hosted_root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for rel_path in plan.export_paths:
        src = hosted_root / rel_path
        dst = output_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    normalize_package_files(output_dir)
    write_candidate_governance(output_dir)

    manifest = {
        "source": "agentengine-hosted-ui",
        "targetRepository": "https://github.com/kingsoftcloud/ksadk-web",
        "publicDemo": KSADK_WEB_PAGES_URL,
        "exportPathCount": len(plan.export_paths),
        "generatedCandidateFiles": ["LICENSE", *sorted(GENERATED_CANDIDATE_FILES)],
        "hostedOnlyTestsExcluded": plan.hosted_only_tests,
        "ksadkOnlyTestsExcluded": plan.ksadk_only_tests,
        "forbiddenPathsExcluded": plan.forbidden_paths_seen,
    }
    (output_dir / "export-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def format_summary(plan: ExportPlan) -> list[str]:
    return [
        f"ok: {str(plan.ok).lower()}",
        f"export paths: {len(plan.export_paths)}",
        f"hosted-only tests excluded: {', '.join(plan.hosted_only_tests) or '-'}",
        f"ksadk-only tests excluded: {', '.join(plan.ksadk_only_tests) or '-'}",
        f"forbidden paths seen but excluded: {len(plan.forbidden_paths_seen)}",
    ]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosted-root", type=Path, default=DEFAULT_HOSTED_ROOT)
    parser.add_argument("--ksadk-web-ui", type=Path, default=DEFAULT_KSADK_WEB_UI)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--summary", action="store_true", help="print a concise human-readable summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    plan = build_export_plan(hosted_root=args.hosted_root, ksadk_web_ui=args.ksadk_web_ui)

    if plan.ok and args.output_dir:
        copy_export(plan, args.output_dir)

    payload = plan.to_dict()
    if args.output_dir:
        payload["output_dir"] = str(args.output_dir.resolve())
        payload["generatedCandidateFiles"] = ["LICENSE", *sorted(GENERATED_CANDIDATE_FILES)]

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(format_summary(plan)))
        for violation in plan.violations:
            print(f"violation: {violation}", file=sys.stderr)

    return 0 if plan.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
