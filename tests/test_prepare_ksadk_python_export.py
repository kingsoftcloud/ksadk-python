from __future__ import annotations

import importlib.util
import json
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
        "contracts/plugin/v1/manifest.json",
        "contracts/conversation/v1/manifest.json",
        "contracts/scheduler/v1/manifest.json",
        ".github/BRANCH_PROTECTION.md",
        ".github/workflows/publish-pypi.yml",
        "scripts/build_alias_distribution.py",
        "docs/public-release-workflow.md",
        "docs/reference/ksadk环境变量参考.md",
        "scripts/check_release_version.py",
        "scripts/open_source_audit.py",
        "scripts/release_preflight.py",
        "scripts/public_secret_audit.py",
        "tests/compat/test_release_legacy_compat.py",
        "tests/e2e/fixtures/codex-marketplace/.agents/plugins/marketplace.json",
        "tests/e2e/fixtures/codex-marketplace/plugins/ksadk-bridge-e2e/skills/bridge-check/SKILL.md",
        "tests/e2e/test_dsh_managed_toolchain_e2e.py",
        "tests/fixtures/dsh-node-agent-provider/provider-host.mjs",
        "tests/plugins/test_dsh_node_provider_e2e.py",
        "tests/events/fixtures/runtime_projection_golden.json",
        "tests/packaging/test_release_preflight.py",
        "tests/studio/e2e/conversation_items_browser_e2e.py",
        "tests/studio/e2e/studio_browser_smoke.py",
        "tests/studio/e2e/studio_e2e_support.py",
        "tests/studio/e2e/studio_responsive_smoke.py",
        "tests/studio/test_style_system.py",
        "tests/test_config_env_registry.py",
        "tests/runners/test_adapter_contract.py",
        "ksadk/studio/react-ui/package.json",
        "ksadk/studio/react-ui/package-lock.json",
    }

    assert required_paths <= set(plan.export_paths)


def test_export_plan_keeps_build_inputs_and_excludes_generated_assets():
    module = _load_module()
    assert module.is_excluded("ksadk/studio/react-ui/src/App.tsx") is False
    assert module.is_excluded("ksadk/studio/react-ui/package-lock.json") is False
    for path in ("ksadk/studio/static/index.html", "ksadk/server/static/assets/app.js",
                 "ksadk/studio/react-ui/node_modules/react/index.js"):
        assert module.is_excluded(path) is True
    plan = module.build_export_plan(Path(__file__).resolve().parents[1])
    assert not any(p.startswith(("ksadk/server/static/", "ksadk/studio/static/"))
                   for p in plan.export_paths)


def test_export_manifest_attests_policy_without_exposing_internal_inventory(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_module()
    repo_root = tmp_path / "source"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("public\n", encoding="utf-8")
    plan = module.ExportPlan(
        ok=True,
        repo_root=str(repo_root),
        target_repository=module.TARGET_REPOSITORY,
        documentation=module.DOCUMENTATION_URL,
        export_paths=["README.md"],
        excluded_paths=["docs/internal/private-plan.md"],
        violations=[],
    )
    monkeypatch.setattr(
        module,
        "git_source_provenance",
        lambda _root: ("a" * 40, "clean"),
    )

    output_dir = tmp_path / "public"
    module.copy_export(plan, output_dir)
    manifest = json.loads(
        (output_dir / "export-manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["schemaVersion"] == 1
    assert manifest["sourceCommit"] == "a" * 40
    assert manifest["sourceTree"] == "clean"
    assert manifest["exportPathCount"] == 1
    assert manifest["exportPolicy"]["mode"] == "allowlist"
    assert manifest["exportPolicy"]["sha256"].isalnum()
    assert "excludedPaths" not in manifest
    assert "excludedPathCount" not in manifest
    assert "includePolicy" not in manifest
    assert "private-plan" not in json.dumps(manifest)
