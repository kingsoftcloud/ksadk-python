from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "prepare_ksadk_web_export.py"


def _load_export_module():
    spec = importlib.util.spec_from_file_location("prepare_ksadk_web_export", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str = "ok\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_ui_tree(root: Path, *, hosted: bool) -> None:
    for file_name in [
        ".gitignore",
        "README.md",
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
        "public/favicon.svg",
        "public/icons.svg",
        "src/App.tsx",
        "src/main.tsx",
        "src/App.css",
        "src/index.css",
    ]:
        _write(root / file_name, f"{file_name}\n")

    package_json = {
        "name": "web-ui",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {
            "dev": "vite",
            "build": "vite build && node scripts/sync-static.mjs",
            "build:hosted": "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted",
            "build:all": "npm run build && npm run build:hosted",
            "test": "vitest run src",
        },
    }
    (root / "package.json").write_text(
        json.dumps(package_json, indent=2) + "\n", encoding="utf-8"
    )
    package_lock = {
        "name": "web-ui",
        "version": "0.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": {
                "name": "web-ui",
                "version": "0.0.0",
            }
        },
    }
    (root / "package-lock.json").write_text(
        json.dumps(package_lock, indent=2) + "\n", encoding="utf-8"
    )

    common_tests = [
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
    ]
    for file_name in common_tests:
        _write(root / "tests" / file_name, f"{file_name}\n")

    if hosted:
        _write(root / "Dockerfile")
        _write(root / "nginx.conf")
        _write(root / "deploy/helm/agentengine-hosted-ui/values.yaml")
        _write(root / "dist-hosted/index.html")
        _write(root / "dist/index.html")
        _write(root / "node_modules/.package-lock.json")
        _write(root / "tests/helm-contract.test.mjs")
        _write(root / "tests/makefile-contract.test.mjs")
    else:
        _write(root / "dist-hosted/index.html")
        _write(root / "scripts/sync-static.mjs")
        _write(root / "tests/hosted-ui-sync.test.mjs")
        _write(root / "tests/sync-static.test.mjs")


def test_default_hosted_root_points_to_workspace_sibling_repo():
    exporter = _load_export_module()

    assert exporter.DEFAULT_HOSTED_ROOT.name == "agentengine-hosted-ui"
    assert exporter.DEFAULT_HOSTED_ROOT.parent.name in {"agentengine", "agent-sdk"}


def test_resolve_default_hosted_root_supports_nested_workspace(tmp_path):
    exporter = _load_export_module()
    hosted_root = tmp_path / "agentengine" / "agentengine-hosted-ui"
    repo_root = tmp_path / "agentengine" / "ksadk-python"
    hosted_root.mkdir(parents=True)
    repo_root.mkdir(parents=True)

    assert exporter.resolve_default_hosted_root(repo_root) == hosted_root


def test_resolve_default_hosted_root_supports_flat_workspace(tmp_path):
    exporter = _load_export_module()
    hosted_root = tmp_path / "agentengine-hosted-ui"
    repo_root = tmp_path / "ksadk-python"
    hosted_root.mkdir(parents=True)
    repo_root.mkdir(parents=True)

    assert exporter.resolve_default_hosted_root(repo_root) == hosted_root


def test_export_plan_selects_shared_files_and_excludes_consumer_shells(tmp_path):
    exporter = _load_export_module()
    hosted_root = tmp_path / "agentengine-hosted-ui"
    ksadk_web_ui = tmp_path / "ksadk-python" / "ksadk" / "server" / "web-ui"
    _make_ui_tree(hosted_root, hosted=True)
    _make_ui_tree(ksadk_web_ui, hosted=False)

    plan = exporter.build_export_plan(hosted_root=hosted_root, ksadk_web_ui=ksadk_web_ui)

    assert plan.ok is True
    assert "src/App.tsx" in plan.export_paths
    assert "public/favicon.svg" in plan.export_paths
    assert "tests/capabilities.test.mjs" in plan.export_paths
    assert "tests/helm-contract.test.mjs" not in plan.export_paths
    assert "tests/makefile-contract.test.mjs" not in plan.export_paths
    assert "tests/hosted-ui-sync.test.mjs" not in plan.export_paths
    assert "tests/sync-static.test.mjs" not in plan.export_paths
    assert "Dockerfile" not in plan.export_paths
    assert "nginx.conf" not in plan.export_paths
    assert "deploy/helm/agentengine-hosted-ui/values.yaml" not in plan.export_paths
    assert "dist-hosted/index.html" not in plan.export_paths
    assert "node_modules/.package-lock.json" not in plan.export_paths
    assert plan.hosted_only_tests == ["helm-contract.test.mjs", "makefile-contract.test.mjs"]
    assert plan.ksadk_only_tests == ["hosted-ui-sync.test.mjs", "sync-static.test.mjs"]


def test_export_plan_reports_source_mismatch(tmp_path):
    exporter = _load_export_module()
    hosted_root = tmp_path / "agentengine-hosted-ui"
    ksadk_web_ui = tmp_path / "ksadk-python" / "ksadk" / "server" / "web-ui"
    _make_ui_tree(hosted_root, hosted=True)
    _make_ui_tree(ksadk_web_ui, hosted=False)
    (ksadk_web_ui / "src" / "App.tsx").write_text("changed\n", encoding="utf-8")

    plan = exporter.build_export_plan(hosted_root=hosted_root, ksadk_web_ui=ksadk_web_ui)

    assert plan.ok is False
    assert any("src/App.tsx" in violation for violation in plan.violations)


def test_cli_writes_candidate_export_without_forbidden_files(tmp_path):
    hosted_root = tmp_path / "agentengine-hosted-ui"
    ksadk_web_ui = tmp_path / "ksadk-python" / "ksadk" / "server" / "web-ui"
    output_dir = tmp_path / "ksadk-web-export"
    _make_ui_tree(hosted_root, hosted=True)
    _make_ui_tree(ksadk_web_ui, hosted=False)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--hosted-root",
            str(hosted_root),
            "--ksadk-web-ui",
            str(ksadk_web_ui),
            "--output-dir",
            str(output_dir),
            "--json",
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["generatedCandidateFiles"]
    assert (output_dir / "src" / "App.tsx").is_file()
    assert (output_dir / "tests" / "capabilities.test.mjs").is_file()
    assert (output_dir / "README.md").is_file()
    assert (output_dir / "LICENSE").is_file()
    assert (output_dir / "SECURITY.md").is_file()
    assert (output_dir / "CONTRIBUTING.md").is_file()
    assert (output_dir / ".github" / "workflows" / "ci.yml").is_file()
    assert (output_dir / ".github" / "workflows" / "pages.yml").is_file()
    assert not (output_dir / "Dockerfile").exists()
    assert not (output_dir / "nginx.conf").exists()
    assert not (output_dir / "deploy").exists()
    assert not (output_dir / "dist-hosted").exists()
    assert not (output_dir / "node_modules").exists()
    assert not (output_dir / "tests" / "helm-contract.test.mjs").exists()
    assert not (output_dir / "tests" / "hosted-ui-sync.test.mjs").exists()

    package_json = json.loads((output_dir / "package.json").read_text(encoding="utf-8"))
    assert package_json["name"] == "@kingsoftcloud/ksadk-web"
    assert package_json["version"] == "0.1.0"
    assert package_json["license"] == "Apache-2.0"
    assert package_json["homepage"] == "https://kingsoftcloud.github.io/ksadk-web/"
    assert "private" not in package_json
    assert package_json["scripts"]["build"] == "npm run build:ksadk"
    assert package_json["scripts"]["build:ksadk"] == "VITE_BASE_PATH=./ vite build --outDir dist-ksadk"
    assert package_json["scripts"]["build:hosted"] == "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted"
    assert "sync-static.mjs" not in json.dumps(package_json)

    package_lock = json.loads((output_dir / "package-lock.json").read_text(encoding="utf-8"))
    assert package_lock["name"] == "@kingsoftcloud/ksadk-web"
    assert package_lock["packages"][""]["name"] == "@kingsoftcloud/ksadk-web"

    manifest = json.loads((output_dir / "export-manifest.json").read_text(encoding="utf-8"))
    assert manifest["publicDemo"] == "https://kingsoftcloud.github.io/ksadk-web/"


def test_cli_summary_mode_prints_review_friendly_output(tmp_path):
    hosted_root = tmp_path / "agentengine-hosted-ui"
    ksadk_web_ui = tmp_path / "ksadk-python" / "ksadk" / "server" / "web-ui"
    _make_ui_tree(hosted_root, hosted=True)
    _make_ui_tree(ksadk_web_ui, hosted=False)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--hosted-root",
            str(hosted_root),
            "--ksadk-web-ui",
            str(ksadk_web_ui),
            "--summary",
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr
    assert "ok: true" in result.stdout
    assert "export paths:" in result.stdout
    assert "hosted-only tests excluded: helm-contract.test.mjs, makefile-contract.test.mjs" in result.stdout
    assert "ksadk-only tests excluded: hosted-ui-sync.test.mjs, sync-static.test.mjs" in result.stdout
    assert "export_paths" not in result.stdout
    assert "src/App.tsx" not in result.stdout
