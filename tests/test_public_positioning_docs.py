from __future__ import annotations

from pathlib import Path
import tomllib
import yaml


ROOT = Path(__file__).resolve().parents[1]


class MkdocsTestLoader(yaml.SafeLoader):
    pass


def _ignore_python_name(loader: MkdocsTestLoader, node: yaml.Node):
    return loader.construct_scalar(node)


MkdocsTestLoader.add_constructor(
    "tag:yaml.org,2002:python/name:pymdownx.superfences.fence_code_format",
    _ignore_python_name,
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _public_markdown_and_config_files() -> list[Path]:
    files = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "README.en.md",
        ROOT / "CHANGELOG.md",
        ROOT / "mkdocs.yml",
        ROOT / "pyproject.toml",
    ]
    files.extend(sorted((ROOT / "public-docs").rglob("*.md")))
    return files


def test_readmes_position_ksadk_as_runtime_platform():
    expected_sections = (
        "Build agents once. Run them anywhere.",
        "Agent Runtime Platform",
        "Why KsADK",
        "30 秒快速体验",
        "Architecture",
        "Comparison",
        "Observability",
        "Deployment",
        "Community",
    )
    for relative_path in ("README.md", "README.zh-CN.md"):
        text = _read(relative_path)
        for expected in expected_sections:
            assert expected in text, f"{relative_path} missing {expected}"
        assert "Agent Development Kit" not in text
        assert "KSADK_SKILL_SERVICE_REGION=pre-online" not in text


def test_english_readme_positions_ksadk_as_runtime_platform():
    text = _read("README.en.md")
    expected_sections = (
        "Build agents once. Run them anywhere.",
        "Agent Runtime Platform",
        "Why KsADK",
        "30 Seconds Quick Start",
        "Architecture",
        "Comparison",
        "Observability",
        "Deployment",
        "Community",
    )
    for expected in expected_sections:
        assert expected in text
    assert "Agent Development Kit" not in text
    assert "KSADK_SKILL_SERVICE_REGION=pre-online" not in text


def test_docs_homepage_uses_runtime_platform_information_architecture():
    zh = _read("public-docs/index.md")
    en = _read("public-docs/index.en.md")
    for text in (zh, en):
        for expected in (
            "Build agents once. Run them anywhere.",
            "Agent Runtime Platform",
            "Why KsADK",
            "Comparison",
            "OpenTelemetry",
            "Hermes",
            "OpenClaw",
        ):
            assert expected in text
        assert "Agent Development Kit" not in text
        assert "成熟 Agent SDK" not in text


def test_public_navigation_is_task_oriented():
    mkdocs = _read("mkdocs.yml")
    for expected in ("Getting Started", "Build", "Run", "Deploy", "Observe", "Extend", "Reference"):
        assert expected in mkdocs
    assert "快速开始: Quick Start" in mkdocs
    assert "Kingsoft Cloud Agent Development Kit" not in mkdocs
    assert "金山云智能体开发套件" not in mkdocs


def test_english_navigation_translates_all_chinese_labels():
    config = yaml.load(_read("mkdocs.yml"), Loader=MkdocsTestLoader)
    translations = (
        config["plugins"][1]["i18n"]["languages"][1]["nav_translations"]
    )

    def iter_labels(items):
        for item in items:
            if isinstance(item, str):
                yield Path(item).stem
            elif isinstance(item, dict):
                for label, children in item.items():
                    yield str(label)
                    if isinstance(children, list):
                        yield from iter_labels(children)

    missing = sorted(
        label
        for label in iter_labels(config["nav"])
        if any("\u4e00" <= character <= "\u9fff" for character in label)
        and label not in translations
    )

    assert missing == []


def test_public_materials_do_not_publish_environment_specific_release_words():
    forbidden = (
        "pre-online",
        "内部预发 endpoint",
        "internal pre-release endpoints",
        "X-Ksc-Region",
        "X-KSC-CUSTOM-SOURCE",
        "agent-api-pre",
        "kspmas-internal",
        "aicp.inner.api",
        "maicp.inner",
    )
    for path in _public_markdown_and_config_files():
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(ROOT)
        for fragment in forbidden:
            assert fragment not in text, f"{relative_path} contains {fragment}"


def test_package_metadata_is_runtime_platform_positioned_for_patch_candidate():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    init_text = _read("ksadk/__init__.py")
    version_text = _read("ksadk/version.py")

    assert pyproject["project"]["version"] == "0.6.4"
    assert 'VERSION = "0.6.4"' in version_text
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


def test_patch_version_changelog_is_unreleased_until_user_review():
    changelog = _read("CHANGELOG.md")
    assert "## [0.6.4] - Unreleased" in changelog
    assert "用户 review 通过前" in changelog
    assert "不创建 tag" in changelog
    assert "不发布 GitHub Release" in changelog
    assert "不上传 PyPI" in changelog
