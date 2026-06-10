from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "prepare_ksadk_python_export.py"
AUDIT_SCRIPT_PATH = REPO_ROOT / "scripts" / "open_source_audit.py"


def _load_export_module():
    spec = importlib.util.spec_from_file_location("prepare_ksadk_python_export", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str = "ok\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_git_repo(root: Path) -> None:
    files = {
        "README.md": "# KSADK\n",
        "README.en.md": "# KSADK\n",
        "README.zh-CN.md": "# KSADK\n",
        "LICENSE": "Apache License\n",
        "mkdocs.yml": "site_name: KSADK\n",
        "pyproject.toml": "[project]\nname = \"ksadk\"\n",
        "ksadk/__init__.py": "\n",
        "ksadk/server/static/index.html": "<!doctype html>\n",
        "ksadk/server/web-ui/src/App.tsx": "export default function App() { return null }\n",
        "ksadk/server/web-ui/package.json": "{\"name\":\"web-ui\"}\n",
        "ksadk/server/web-ui/dist-hosted/index.html": "<!doctype html>\n",
        "ksadk_runtime_common/__init__.py": "\n",
        "ksadk_runtime_common/schemas/event.json": "{}\n",
        "tests/test_open_source_audit.py": "def test_public():\n    assert True\n",
        "tests/test_public_positioning_docs.py": "def test_public_docs():\n    assert True\n",
        "tests/test_check_publication_state.py": "def test_publication_state():\n    assert True\n",
        "tests/test_tracing_setup_otlp.py": "def test_tracing_public():\n    assert True\n",
        "tests/test_deploy_integration.py": "def test_internal():\n    assert True\n",
        "tests/snapshots/help_snapshots.txt": "internal snapshot\n",
        "public-docs/index.md": "# Public docs\n",
        "public-docs/assets/ksadk-runtime-platform-hero.png": "hero\n",
        "public-docs/assets/ksadk-web-ui-screenshot.png": "screenshot\n",
        "public-docs/assets/ksadk-runtime-architecture.svg": "<svg></svg>\n",
        "public-docs/assets/ksadk-runtime-architecture.png": "png\n",
        "public-docs/assets/ksadk-local-debugging-demo.gif": "gif\n",
        "scripts/open_source_audit.py": "print('audit')\n",
        "scripts/audit_release_artifacts.py": "print('dist audit')\n",
        "scripts/check_publication_state.py": "print('publication')\n",
        "scripts/generate_public_assets.py": "print('assets')\n",
        "scripts/prepare_ksadk_python_export.py": "print('export')\n",
        "scripts/prepare_ksadk_web_export.py": "print('web export')\n",
        "docs/internal/release-secret.md": "internal\n",
        "docs/archive/old.md": "old internal doc\n",
        "examples/smart_assistant_adk/.env.example": "OPENAI_API_BASE=http://kspmas.ksyun.com/v1\n",
        "deploy/hermes/Dockerfile": "FROM internal\n",
        "deploy/openclaw/Dockerfile": "FROM internal\n",
        "skills/agentengine-cluster-debug/SKILL.md": "kubeconfig\n",
        "Makefile.openclaw": "DOCKER_REGISTRY ?= ghcr.io\n",
        "Makefile.promo.Dockerfile": "FROM internal\n",
        "CLAUDE.md": "public assistant notes\n",
        "AGENTS.md": "public contributor notes\n",
        ".env.example": "OPENAI_API_BASE=https://kspmas.ksyun.com/v1\n",
        ".zread/wiki/current.md": "generated\n",
        "site/index.html": "generated\n",
        "dist/ksadk-0.1.0.whl": "wheel\n",
        "build/lib/ksadk/__init__.py": "\n",
        "ksadk.egg-info/PKG-INFO": "metadata\n",
        ".pypirc": "token\n",
        ".pypirc.example": "token placeholder\n",
        "deploy/helm/ksadk-docs/values.yaml": "internal route\n",
        "Dockerfile.docs": "internal docs deploy\n",
        "__pycache__/module.pyc": "cache\n",
    }
    for rel_path, text in files.items():
        _write(root / rel_path, text)

    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "init",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )


def test_export_plan_selects_public_candidate_files_and_excludes_local_artifacts(tmp_path):
    exporter = _load_export_module()
    _make_git_repo(tmp_path)

    plan = exporter.build_export_plan(tmp_path)

    assert plan.ok is True
    assert "README.md" in plan.export_paths
    assert "README.en.md" in plan.export_paths
    assert "README.zh-CN.md" in plan.export_paths
    assert "CLAUDE.md" in plan.export_paths
    assert "AGENTS.md" in plan.export_paths
    assert "ksadk/__init__.py" in plan.export_paths
    assert "ksadk/server/static/index.html" in plan.export_paths
    assert "ksadk/server/web-ui/src/App.tsx" not in plan.export_paths
    assert "ksadk/server/web-ui/package.json" not in plan.export_paths
    assert "ksadk/server/web-ui/dist-hosted/index.html" not in plan.export_paths
    assert "ksadk_runtime_common/__init__.py" in plan.export_paths
    assert "ksadk_runtime_common/schemas/event.json" in plan.export_paths
    assert "tests/test_open_source_audit.py" in plan.export_paths
    assert "tests/test_public_positioning_docs.py" in plan.export_paths
    assert "tests/test_check_publication_state.py" in plan.export_paths
    assert "tests/test_tracing_setup_otlp.py" in plan.export_paths
    assert "public-docs/index.md" in plan.export_paths
    assert "public-docs/assets/ksadk-runtime-platform-hero.png" in plan.export_paths
    assert "public-docs/assets/ksadk-web-ui-screenshot.png" in plan.export_paths
    assert "public-docs/assets/ksadk-runtime-architecture.svg" in plan.export_paths
    assert "public-docs/assets/ksadk-runtime-architecture.png" in plan.export_paths
    assert "public-docs/assets/ksadk-local-debugging-demo.gif" in plan.export_paths
    assert "docs/release-checklist.md" not in plan.export_paths
    assert "docs/ksadk开源准备计划.md" not in plan.export_paths
    assert "scripts/open_source_audit.py" in plan.export_paths
    assert "scripts/audit_release_artifacts.py" in plan.export_paths
    assert "scripts/check_publication_state.py" in plan.export_paths
    assert "scripts/generate_public_assets.py" in plan.export_paths
    assert "scripts/prepare_ksadk_python_export.py" in plan.export_paths
    assert "scripts/prepare_ksadk_web_export.py" in plan.export_paths
    assert "tests/test_deploy_integration.py" not in plan.export_paths
    assert "tests/snapshots/help_snapshots.txt" not in plan.export_paths
    assert "docs/internal/release-secret.md" not in plan.export_paths
    assert "docs/archive/old.md" not in plan.export_paths
    assert "examples/smart_assistant_adk/.env.example" not in plan.export_paths
    assert "deploy/hermes/Dockerfile" not in plan.export_paths
    assert "deploy/openclaw/Dockerfile" not in plan.export_paths
    assert "skills/agentengine-cluster-debug/SKILL.md" not in plan.export_paths
    assert "Makefile.openclaw" not in plan.export_paths
    assert "Makefile.promo.Dockerfile" not in plan.export_paths
    assert ".env.example" not in plan.export_paths
    assert ".zread/wiki/current.md" not in plan.export_paths
    assert "site/index.html" not in plan.export_paths
    assert "dist/ksadk-0.1.0.whl" not in plan.export_paths
    assert "build/lib/ksadk/__init__.py" not in plan.export_paths
    assert "ksadk.egg-info/PKG-INFO" not in plan.export_paths
    assert ".pypirc" not in plan.export_paths
    assert ".pypirc.example" not in plan.export_paths
    assert "deploy/helm/ksadk-docs/values.yaml" not in plan.export_paths
    assert "Dockerfile.docs" not in plan.export_paths
    assert "__pycache__/module.pyc" not in plan.export_paths
    assert ".pypirc" in plan.excluded_paths
    assert "deploy/hermes/Dockerfile" in plan.excluded_paths
    assert "skills/agentengine-cluster-debug/SKILL.md" in plan.excluded_paths


def test_cli_writes_clean_export_candidate_and_manifest(tmp_path):
    source_root = tmp_path / "source"
    output_dir = tmp_path / "ksadk-python-export"
    source_root.mkdir()
    _make_git_repo(source_root)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--repo-root",
            str(source_root),
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
    assert payload["outputDir"] == str(output_dir.resolve())
    assert (output_dir / "README.md").is_file()
    assert (output_dir / "README.en.md").is_file()
    assert (output_dir / "README.zh-CN.md").is_file()
    assert (output_dir / "CLAUDE.md").is_file()
    assert (output_dir / "AGENTS.md").is_file()
    assert (output_dir / "ksadk" / "__init__.py").is_file()
    assert (output_dir / "ksadk" / "server" / "static" / "index.html").is_file()
    assert not (output_dir / "ksadk" / "server" / "web-ui").exists()
    assert (output_dir / "ksadk_runtime_common" / "__init__.py").is_file()
    assert (output_dir / "public-docs" / "index.md").is_file()
    assert (output_dir / "public-docs" / "assets" / "ksadk-runtime-platform-hero.png").is_file()
    assert (output_dir / "public-docs" / "assets" / "ksadk-web-ui-screenshot.png").is_file()
    assert (output_dir / "public-docs" / "assets" / "ksadk-runtime-architecture.svg").is_file()
    assert (output_dir / "public-docs" / "assets" / "ksadk-runtime-architecture.png").is_file()
    assert (output_dir / "public-docs" / "assets" / "ksadk-local-debugging-demo.gif").is_file()
    assert (output_dir / "export-manifest.json").is_file()
    assert not (output_dir / ".pypirc").exists()
    assert not (output_dir / "docs" / "internal").exists()
    assert not (output_dir / "docs" / "archive").exists()
    assert not (output_dir / "examples").exists()
    assert not (output_dir / ".zread").exists()
    assert not (output_dir / "site").exists()
    assert not (output_dir / "dist").exists()
    assert not (output_dir / "deploy").exists()
    assert not (output_dir / "skills").exists()
    assert not (output_dir / "Makefile.openclaw").exists()
    assert not (output_dir / "Makefile.promo.Dockerfile").exists()
    assert not (output_dir / ".env.example").exists()
    assert not (output_dir / "deploy" / "helm" / "ksadk-docs").exists()

    manifest = json.loads((output_dir / "export-manifest.json").read_text(encoding="utf-8"))
    assert manifest["targetRepository"] == "https://github.com/kingsoftcloud/ksadk-python"
    assert manifest["documentation"] == "https://kingsoftcloud.github.io/ksadk-python/"
    assert manifest["exportPathCount"] == len(payload["export_paths"])
    assert ".pypirc" in manifest["excludedPaths"]
    assert "deploy/hermes/Dockerfile" in manifest["excludedPaths"]
    assert "tests/test_deploy_integration.py" in manifest["excludedPaths"]
    assert "examples/smart_assistant_adk/.env.example" in manifest["excludedPaths"]

    audit = subprocess.run(
        [
            sys.executable,
            str(AUDIT_SCRIPT_PATH),
            "--target",
            "public-repo",
            "--root",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert audit.returncode == 0, audit.stdout + audit.stderr


def test_cli_summary_mode_prints_review_friendly_output(tmp_path):
    _make_git_repo(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--repo-root",
            str(tmp_path),
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
    assert "target repository: https://github.com/kingsoftcloud/ksadk-python" in result.stdout
    assert "documentation: https://kingsoftcloud.github.io/ksadk-python/" in result.stdout
    assert "export paths:" in result.stdout
    assert "excluded paths:" in result.stdout
