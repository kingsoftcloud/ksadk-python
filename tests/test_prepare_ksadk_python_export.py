from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_ksadk_python_export.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepare_ksadk_python_export", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_export_plan_requires_fumadocs_site_not_legacy_mkdocs():
    module = _load_module()

    assert "mkdocs.yml" not in module.ROOT_EXPORT_FILES
    assert "docs-site/" in module.EXPORT_PREFIXES
    assert "public-docs/" not in module.EXPORT_PREFIXES
    assert "docs-site/package.json" in module.REQUIRED_PUBLIC_FILES
    assert "docs-site/pnpm-lock.yaml" in module.REQUIRED_PUBLIC_FILES


def test_export_plan_includes_public_preflight_contract_files():
    module = _load_module()
    plan = module.build_export_plan(Path(__file__).resolve().parents[1])

    required_paths = {
        ".github/BRANCH_PROTECTION.md",
        ".github/workflows/publish-pypi.yml",
        "docs/reference/ksadk环境变量参考.md",
        "scripts/check_release_version.py",
        "scripts/open_source_audit.py",
        "scripts/public_secret_audit.py",
        "tests/test_config_env_registry.py",
    }

    assert required_paths <= set(plan.export_paths)
