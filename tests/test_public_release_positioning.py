from __future__ import annotations

from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_public_readme_positions_ksadk_as_runtime_platform():
    readme = _read("README.md")
    for expected in (
        "Kingsoft Cloud Agent Development Kit",
        "金山云智能体开发套件",
        "构建、部署、调试、观测企业级 AI 智能体的一站式云原生框架",
        "OpenClaw",
        "Hermes",
        "30 秒快速体验",
        "为什么需要 KsADK",
        "架构",
        "文档与样例",
        "相关项目",
        "参与贡献",
        "ksadk-runtime-platform-hero-wide.png",
        "ksadk-web-ui-screenshot.png",
        "ksadk-local-debugging-demo.gif",
        "ksadk-runtime-architecture.png",
    ):
        assert expected in readme

    assert "KSADK_SKILL_SERVICE_REGION=pre-online" not in readme
    assert "```mermaid" not in readme
    assert "当前版本：" not in readme
    assert "发布版本：" not in readme
    assert "## 0.6." not in readme


def test_public_readme_language_variants_keep_homepage_shape():
    zh_readme = _read("README.zh-CN.md")
    en_readme = _read("README.en.md")

    for text in (zh_readme, en_readme):
        assert "Kingsoft Cloud Agent Development Kit" in text
        assert "ksadk-runtime-platform-hero-wide.png" in text
        assert "ksadk-web-ui-screenshot.png" in text
        assert "ksadk-local-debugging-demo.gif" in text
        assert "ksadk-runtime-architecture.png" in text
        assert "发布版本：" not in text
        assert "## 0.6." not in text


def test_public_metadata_uses_runtime_platform_positioning():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    init_text = _read("ksadk/__init__.py")
    version_text = _read("ksadk/version.py")

    assert pyproject["project"]["version"] == "0.6.9"
    assert 'VERSION = "0.6.9"' in version_text
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


def test_changelog_marks_0_6_7_ready_for_authorized_release():
    changelog = _read("CHANGELOG.md")

    assert "## [0.6.7] - 2026-06-26" in changelog
    assert "ResumeMode" in changelog
    assert "ListSessionCheckpoints" in changelog
    assert "GetCheckpointResumePreview" in changelog
    assert "PreviewCheckpointResume" not in changelog
    assert "SubscribeRunEvents" in changelog
    assert "CancelRun" in changelog
    assert "PyPI Trusted Publishing" in changelog
    assert "KSADK_PACKAGE_SPEC=ksadk==0.6.7" in changelog


def test_pypi_publish_workflow_uses_trusted_publishing_and_bundles_ksadk_web():
    workflow = _read(".github/workflows/publish-pypi.yml")
    ci_workflow = _read(".github/workflows/ci.yml")
    makefile = _read("Makefile")

    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "push:" not in workflow
    assert "release:" in workflow
    assert "- published" in workflow
    assert "workflow_dispatch:" in workflow
    assert 'default: "0.2.18"' in workflow
    assert "KSADK_WEB_VERSION: ${{ github.event.inputs.ksadk_web_version || '0.2.18' }}" in workflow
    assert "make sync-ksadk-web-static" in workflow
    assert "make public-preflight" in workflow
    assert "make public-publish-gate" in workflow
    assert "make open-source-audit-dist" in ci_workflow
    assert 'KSADK_WEB_VERSION: "0.2.18"' in ci_workflow
    assert "PUBLIC_KSADK_WEB_VERSION" not in ci_workflow
    assert "KSADK_WEB_VERSION ?= latest" in makefile
    assert "PUBLIC_TEST_TARGETS ?= tests/test_public_release_positioning.py tests/test_config_env_registry.py" in makefile
    assert "public-sync-ksadk-web-static: sync-ksadk-web-static" in makefile
    assert "python3 scripts/open_source_audit.py --target public-repo" in makefile
    assert "open-source-audit-dist:" in makefile
    assert "public-release-approval-check:" in makefile
    assert "public-publish-gate: public-release-approval-check" in makefile
    assert "scripts/check_approval_record.py" in makefile
    assert '--expected-current-commit "$${KSADK_APPROVED_SOURCE_COMMIT:-}"' not in makefile
    assert "public-build-check: clean-dist sync-ksadk-web-static" in makefile
    assert "public-preflight: public-version-gate public-audit sync-ksadk-web-static public-test public-docs-build public-build-check" in makefile
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_public_ci_runs_gitleaks_and_documents_branch_protection():
    secret_workflow = _read(".github/workflows/secret-patterns.yml")
    branch_protection = _read(".github/BRANCH_PROTECTION.md")
    approval_record = _read("docs/maintainer-approval-record.md")

    assert 'GITLEAKS_VERSION: "8.28.0"' in secret_workflow
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in secret_workflow
    assert "/tmp/gitleaks detect --source ." in secret_workflow
    assert "fetch-depth: 0" in secret_workflow
    assert "python3 scripts/open_source_audit.py --target public-repo" in secret_workflow
    assert "Require a pull request before merging" in branch_protection
    assert "CI / test" in branch_protection
    assert "Secret Pattern Audit / scan" in branch_protection
    assert "CodeQL / analyze" in branch_protection
    assert "pypi environment" in branch_protection
    assert "Branch protection and publish environment are configured" in approval_record


def test_public_release_approval_template_tracks_current_version():
    approval_record = _read("docs/maintainer-approval-record.md")

    assert "| Python package version | 0.6.9 |" in approval_record
    assert "make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.6.9" in approval_record


def test_source_repository_does_not_track_generated_ksadk_web_static():
    gitignore = _read(".gitignore")
    pyproject = _read("pyproject.toml")
    if (ROOT / ".git").exists():
        web_ui_files = subprocess.run(
            ["git", "ls-files", "ksadk/server/web-ui/**"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
    else:
        web_ui_files = "\n".join(str(path.relative_to(ROOT)) for path in (ROOT / "ksadk/server/web-ui").glob("**/*") if path.is_file())

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
    )
    for relative_path in (
        "README.md",
        "CHANGELOG.md",
        "pyproject.toml",
        "ksadk/__init__.py",
    ):
        text = _read(relative_path)
        for fragment in forbidden:
            assert fragment not in text, f"{relative_path} contains {fragment}"
