from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare_ksadk_python_export.py"


def test_reviewed_web_manifest_survives_public_git_import(tmp_path):
    """A local-only ignored manifest cannot reproduce a public checkout."""
    root = SCRIPT_PATH.parents[1]
    (tmp_path / ".gitignore").write_bytes((root / ".gitignore").read_bytes())
    vendor = tmp_path / "ksadk/studio/react-ui/vendor"
    vendor.mkdir(parents=True)
    manifest = vendor / "ksadk-web-source.json"
    manifest.write_text('{}\n')
    (vendor / "unreviewed-archive.tgz").write_bytes(b"fixture archive")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=tmp_path, text=True,
    ).split("\0"))
    assert "ksadk/studio/react-ui/vendor/ksadk-web-source.json" in tracked
    assert "ksadk/studio/react-ui/vendor/unreviewed-archive.tgz" not in tracked


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
        "tests/test_public_secret_audit.py",
        "tests/packaging/test_reviewed_web_assets.py",
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
        "ksadk/studio/react-ui/vendor/ksadk-web-source.json",
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


def test_clean_export_contains_actual_desktop_build_dependencies(tmp_path):
    module = _load_module()
    root = SCRIPT_PATH.parents[1]
    plan = module.build_export_plan(root)
    output = tmp_path / "public"
    module.copy_export(plan, output)
    workflow = (output / ".github/workflows/release-studio-app.yml").read_text()
    dependencies = set(re.findall(r"scripts/[\w./-]+\.(?:py|sh|cjs)", workflow))
    dependencies.update(re.findall(r"--config\s+([\w.-]+\.cjs)", workflow))
    dependencies.add("package.json")
    for config in tuple(path for path in dependencies if path.endswith(".cjs")):
        assert (output / config).is_file(), f"workflow build input excluded: {config}"
        dependencies.update(re.findall(r"scripts/[\w./-]+\.(?:cjs|sh|entitlements)",
                                      (output / config).read_text()))
    for path in dependencies:
        assert (output / path).is_file(), f"desktop build input excluded: {path}"
    assert (output / "scripts/prepare_studio_assets.sh").is_file()
    assert (output / "scripts/verify_native_studio_install.py").is_file()


def test_export_contains_ci_tests_and_their_local_imports():
    module = _load_module()
    root = SCRIPT_PATH.parents[1]
    exported = set(module.build_export_plan(root).export_paths)
    gate_sources = (
        root / ".github/workflows/ci.yml",
        root / "scripts/release_preflight.py",
    )
    gate_tests = {
        path
        for source in gate_sources
        for path in re.findall(r"tests/[\w/.-]+\.py", source.read_text())
    }
    assert gate_tests <= exported, f"CI tests excluded: {sorted(gate_tests - exported)}"

    missing_imports = set()
    for path in sorted(exported):
        if not path.startswith("tests/") or not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse((root / path).read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{item.name}" for item in node.names)]
            elif isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            else:
                continue
            for name in names:
                if not name.startswith("tests."):
                    continue
                stem = name.replace(".", "/")
                for dependency in (f"{stem}.py", f"{stem}/__init__.py"):
                    if (root / dependency).is_file() and dependency not in exported:
                        missing_imports.add((path, dependency))
    assert not missing_imports, f"test imports excluded: {sorted(missing_imports)}"


def test_export_keeps_native_recovery_and_shared_test_fixtures():
    module = _load_module()
    exported = set(module.build_export_plan(SCRIPT_PATH.parents[1]).export_paths)
    required = {
        "tests/harness/native/test_partial_summary_process.py",
        "tests/harness/native/test_summary_recovery_process.py",
        "tests/harness/native/test_compaction_reserve_process_recovery.py",
        "tests/harness/native/partial_summary_process_fixture.py",
        "tests/harness/native/summary_recovery_process_fixture.py",
        "tests/harness/native/compaction_reserve_process_fixture.py",
        "tests/studio/native_recovery_lease_fixture.py",
        "tests/studio/test_native_default_install.py",
        "tests/a2a/fixtures/remote_agent/v1/a2a_stream_tool_terminal.jsonl",
        "tests/fixtures/adk_two_llm_outputs.json",
    }
    assert required <= exported, f"release test inputs excluded: {sorted(required - exported)}"


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


def test_public_export_keeps_new_protocol_regressions_and_local_fixtures():
    module = _load_module()
    root = SCRIPT_PATH.parents[1]
    required = {
        "tests/a2ui/test_official_wire.py",
        "tests/agui/test_protocol_contract.py",
        "tests/agui/test_native_cold_resume.py",
        "tests/test_mcp_oauth_local.py",
        "tests/test_mcp_oauth_declarations.py",
        "tests/test_mcp_oauth_documented_graph.py",
        "tests/fixtures/local_oauth_provider.py",
        "tests/a2a/test_control_plane.py",
        "tests/a2a/test_workload_token_public_compat.py",
        "tests/a2a/test_a2a_protocol_e2e.py",
        "tests/a2a/test_context_store.py",
        "tests/a2a/test_native_process_recovery.py",
        "tests/server/test_app_factory_agui.py",
    }
    exported = set(module.build_export_plan(root).export_paths)
    assert required <= exported, f"protocol regressions excluded: {sorted(required - exported)}"
    assert "tests/a2a/fixtures/restored-assets-manifest.json" not in exported
