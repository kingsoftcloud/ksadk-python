from __future__ import annotations

from pathlib import Path
import re
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


PUBLIC_FORBIDDEN_PATTERNS = (
    ("pre_release_region", re.compile(r"\bpre[\W_]*online\b", re.IGNORECASE)),
    ("private_icp_endpoint", re.compile(r"\b\w*icp[.-]inner[.-]api[.-][\w.-]+\b", re.IGNORECASE)),
    ("private_agent_api_endpoint", re.compile(r"\bagent[.-]api[.-]pre\b", re.IGNORECASE)),
    ("private_kspmas_endpoint", re.compile(r"\bkspmas[.-]internal\b", re.IGNORECASE)),
    ("private_region_header", re.compile(r"\bX[-_]K(?:sc|SC)[-_]Region\b")),
    ("private_custom_source_header", re.compile(r"\bX[-_]KSC[-_]CUSTOM[-_]SOURCE\b")),
    (
        "private_review_process",
        re.compile(
            r"\b(?:internal\s+(?:ezone|review\s+gate|maintainer\s+review)|company\s+review)\b",
            re.IGNORECASE,
        ),
    ),
    ("private_review_process_zh", re.compile(r"\u5185\u90e8\s*(?:ezone|review|\u5ba1\u6838)")),
)


def _assert_no_public_sensitive_patterns(relative_path: str, text: str) -> None:
    for label, pattern in PUBLIC_FORBIDDEN_PATTERNS:
        assert not pattern.search(text), f"{relative_path} matches {label}: {pattern.pattern}"


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
        "KSYUN_REGION=cn-beijing-6",
    )
    for relative_path in ("README.md", "README.zh-CN.md"):
        text = _read(relative_path)
        for expected in expected_sections:
            assert expected in text, f"{relative_path} missing {expected}"
        assert "```mermaid" not in text
        assert "```text" in text
        assert "Agent Development Kit" not in text
        _assert_no_public_sensitive_patterns(relative_path, text)
        assert "当前版本：" not in text
        assert "候选版本：`0.6.4`" in text


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
        "KSYUN_REGION=cn-beijing-6",
    )
    for expected in expected_sections:
        assert expected in text
    assert "```mermaid" not in text
    assert "```text" in text
    assert "Agent Development Kit" not in text
    _assert_no_public_sensitive_patterns("README.en.md", text)
    assert "Current version:" not in text
    assert "Candidate version: `0.6.4`" in text


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
            "KSYUN_REGION=cn-beijing-6",
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
    for path in _public_markdown_and_config_files():
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(ROOT)
        _assert_no_public_sensitive_patterns(str(relative_path), text)


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
