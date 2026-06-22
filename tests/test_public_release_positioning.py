from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _changelog_section(version: str) -> str:
    changelog = _read("CHANGELOG.md")
    start_marker = f"## [{version}]"
    start = changelog.index(start_marker)
    next_start = changelog.find("\n## [", start + len(start_marker))
    if next_start == -1:
        return changelog[start:]
    return changelog[start:next_start]


def test_public_readme_positions_ksadk_as_runtime_platform():
    readme = _read("README.md")
    for expected in (
        "Build agents once. Run them anywhere.",
        "Agent Runtime Platform",
        "Why KsADK",
        "30 秒快速体验",
        "Architecture",
        "Comparison",
        "Examples",
        "Deployment",
        "Observability",
        "Documentation",
        "Community",
        "KSYUN_REGION=cn-beijing-6",
    ):
        assert expected in readme

    assert "Agent Development Kit" not in readme
    assert "KSADK_SKILL_SERVICE_REGION=pre-online" not in readme
    assert "```mermaid" not in readme
    assert "```text" in readme
    assert "当前版本：" not in readme
    assert (
        "发布版本：`0.6.6`" in readme
        or "当前源码版本：`0.6.6`。正式发布通过 GitHub Release 和 PyPI Trusted Publishing 提供。" in readme
    )


def test_public_metadata_uses_runtime_platform_positioning():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    init_text = _read("ksadk/__init__.py")
    version_text = _read("ksadk/version.py")

    assert pyproject["project"]["version"] == "0.6.6"
    assert 'VERSION = "0.6.6"' in version_text
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


def test_changelog_marks_0_6_6_ready_for_authorized_release():
    changelog = _changelog_section("0.6.6")

    assert "## [0.6.6] - 2026-06-18" in changelog
    assert "统一模型策略 v1" in changelog
    assert "PyPI Trusted Publishing" in changelog
    assert "GitHub workflow" in changelog
    assert "人工确认" in changelog
    assert "KSADK_PACKAGE_SPEC=ksadk==0.6.6" not in changelog
    assert "agentengine-images" not in changelog


def test_changelog_0_6_6_only_describes_ksadk_release_surface():
    changelog = _changelog_section("0.6.6")
    forbidden = (
        "agentengine-gateway",
        "agentengine-server",
        "agentengine-images",
        "gateway",
        "server",
        "服务端",
        "预发",
        "pre-online",
        "默认镜像",
        "镜像构建",
        "镜像仓库",
        "内部提交",
    )

    for fragment in forbidden:
        assert fragment not in changelog, f"0.6.6 changelog contains {fragment}"


def test_pypi_publish_workflow_uses_trusted_publishing_and_bundles_ksadk_web():
    workflow = _read(".github/workflows/publish-pypi.yml")
    makefile = _read("Makefile")

    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "make public-preflight" in workflow
    assert "PUBLIC_KSADK_WEB_VERSION: ${{ github.event.inputs.ksadk_web_version || '0.2.11' }}" in workflow
    assert "default: \"0.2.11\"" in workflow
    assert "KSADK_WEB_VERSION ?= latest" in makefile
    assert "PUBLIC_KSADK_WEB_VERSION ?= 0.2.11" in makefile
    assert "public-build-check: clean-dist public-sync-ksadk-web-static" in makefile
    assert "public-preflight: public-audit public-sync-ksadk-web-static public-test" in makefile
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_github_public_release_gate_workflows_reference_existing_targets():
    ci_workflow = _read(".github/workflows/ci.yml")
    release_check_workflow = _read(".github/workflows/release-check.yml")
    makefile = _read("Makefile")

    referenced_tests = re.findall(r"(tests/[A-Za-z0-9_./-]+\.py)", ci_workflow)
    assert referenced_tests
    missing_tests = [path for path in referenced_tests if not (ROOT / path).is_file()]
    assert missing_tests == []

    referenced_make_targets = re.findall(r"\bmake\s+([A-Za-z0-9_.-]+)", release_check_workflow)
    assert "open-source-audit-dist" in referenced_make_targets
    missing_targets = [
        target
        for target in referenced_make_targets
        if re.search(rf"^{re.escape(target)}\s*:", makefile, flags=re.MULTILINE) is None
    ]
    assert missing_targets == []

    assert 'PUBLIC_KSADK_WEB_VERSION: "0.2.11"' in ci_workflow
    assert ci_workflow.index("- name: Sync KsADK Web static assets") < ci_workflow.index(
        "- name: Build package artifacts"
    )
    assert ci_workflow.index("- name: Build package artifacts") < ci_workflow.index(
        "- name: Run public release gate tests"
    )


def test_source_repository_does_not_track_generated_ksadk_web_static():
    gitignore = _read(".gitignore")
    pyproject = _read("pyproject.toml")
    web_ui_files = subprocess.run(
        ["git", "ls-files", "ksadk/server/web-ui/**"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout

    assert "ksadk/server/static/**" in gitignore
    assert "ksadk/server/web-ui/" in gitignore
    assert '"server/static/**/*"' in pyproject
    assert "server/web-ui" not in pyproject
    assert web_ui_files == ""


def test_public_release_materials_do_not_include_internal_environment_details():
    forbidden = (
        "KSADK_SKILL_SERVICE_REGION=pre-online",
        "预发",
        "agent-api-pre",
        "kspmas-internal",
        "X-Ksc-Region",
        "X-KSC-CUSTOM-SOURCE",
        "aicp.inner.api",
        "m" + "aicp.",
        "Kingsoft Cloud Agent Development Kit",
    )
    for relative_path in (
        "README.md",
        "CHANGELOG.md",
        "docs/ksadk环境变量参考.md",
        "docs/远程Agent运行时接口说明.md",
        "pyproject.toml",
        "ksadk/__init__.py",
    ):
        text = _read(relative_path)
        for fragment in forbidden:
            assert fragment not in text, f"{relative_path} contains {fragment}"
