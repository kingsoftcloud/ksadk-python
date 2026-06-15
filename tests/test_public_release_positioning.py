from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


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
    assert "候选版本：`0.6.5`" in readme


def test_public_metadata_uses_runtime_platform_positioning():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    init_text = _read("ksadk/__init__.py")
    version_text = _read("ksadk/version.py")

    assert pyproject["project"]["version"] == "0.6.5"
    assert 'VERSION = "0.6.5"' in version_text
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


def test_changelog_marks_0_6_5_unreleased_until_user_review():
    changelog = _read("CHANGELOG.md")

    assert "## [0.6.5] - Unreleased" in changelog
    assert "用户 review 通过前" in changelog
    assert "不创建 tag" in changelog
    assert "不发布 GitHub Release" in changelog
    assert "不上传 PyPI" in changelog


def test_pypi_publish_workflow_uses_trusted_publishing_and_bundles_ksadk_web():
    workflow = _read(".github/workflows/publish-pypi.yml")
    makefile = _read("Makefile")

    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
    assert "make sync-ksadk-web-static" in workflow
    assert "make public-preflight" in workflow
    assert "KSADK_WEB_VERSION ?= latest" in makefile
    assert "public-build-check: clean-dist sync-ksadk-web-static" in makefile
    assert "public-preflight: public-audit sync-ksadk-web-static public-test" in makefile
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_source_repository_does_not_track_generated_ksadk_web_static():
    gitignore = _read(".gitignore")
    pyproject = _read("pyproject.toml")

    assert "ksadk/server/static/**" in gitignore
    assert '"server/static/**/*"' in pyproject
    assert "server/web-ui/dist-hosted" not in pyproject


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
        "pyproject.toml",
        "ksadk/__init__.py",
    ):
        text = _read(relative_path)
        for fragment in forbidden:
            assert fragment not in text, f"{relative_path} contains {fragment}"
