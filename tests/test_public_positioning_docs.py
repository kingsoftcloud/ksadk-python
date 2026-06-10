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
    ("pre_release_region_zh", re.compile(r"\u9884\u53d1")),
    (
        "private_icp_endpoint",
        re.compile(
            r"\b(?!(?:aicp[.-](?:inner|internal)[.-]api[.-]ksyun[.-]com|aicp[.-]api[.-]ksyun[.-]com)\b)"
            r"\w*icp[.-](?:inner|internal)[.-]api[.-][\w.-]+\b",
            re.IGNORECASE,
        ),
    ),
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
        "简体中文（默认）",
        "一次构建 Agent，到处运行。",
        "Agent Runtime Platform",
        "public-docs/assets/ksadk-runtime-platform-hero-wide.png",
        "真实 CLI 截图",
        "为什么需要 KsADK",
        "30 秒快速体验",
        "真实本地 Web UI 演示",
        "public-docs/assets/ksadk-web-ui-screenshot.png",
        "public-docs/assets/ksadk-local-debugging-demo.gif",
        "public-docs/assets/ksadk-runtime-architecture.png",
        "架构",
        "生态定位对比",
        "可观测",
        "文档与样例",
        "相关项目",
        "参与贡献",
    )
    for relative_path in ("README.md", "README.zh-CN.md"):
        text = _read(relative_path)
        for expected in expected_sections:
            assert expected in text, f"{relative_path} missing {expected}"
        assert "```mermaid" not in text
        assert "Agent Development Kit" not in text
        _assert_no_public_sensitive_patterns(relative_path, text)
        assert "当前版本：" not in text
        assert "候选版本：" not in text
        assert "## 0.6.4 重点" not in text
        assert "## 0.6.3 重点" not in text
        assert "repair_markdown" not in text
        assert "[CHANGELOG.md](CHANGELOG.md)" not in text
        assert "https://github.com/kingsoftcloud/ksadk-python/releases" not in text


def test_english_readme_positions_ksadk_as_runtime_platform():
    text = _read("README.en.md")
    expected_sections = (
        "Build agents once. Run them anywhere.",
        "Agent Runtime Platform",
        "public-docs/assets/ksadk-runtime-platform-hero-wide.png",
        "Real KsADK CLI screenshot",
        "Why KsADK",
        "30 Seconds Quick Start",
        "local debugging Web UI",
        "public-docs/assets/ksadk-web-ui-screenshot.png",
        "public-docs/assets/ksadk-local-debugging-demo.gif",
        "public-docs/assets/ksadk-runtime-architecture.png",
        "Architecture",
        "Ecosystem Positioning",
        "Observability",
        "Docs And Examples",
        "Related Projects",
        "Contributing",
    )
    for expected in expected_sections:
        assert expected in text
    assert "```mermaid" not in text
    assert "Agent Development Kit" not in text
    _assert_no_public_sensitive_patterns("README.en.md", text)
    assert "Current version:" not in text
    assert "Candidate version:" not in text
    assert "## 0.6.4 Highlights" not in text
    assert "## 0.6.3 Highlights" not in text
    assert "repair_markdown" not in text
    assert "[CHANGELOG.md](CHANGELOG.md)" not in text
    assert "https://github.com/kingsoftcloud/ksadk-python/releases" not in text


def test_docs_homepage_uses_runtime_platform_information_architecture():
    zh = _read("public-docs/index.md")
    en = _read("public-docs/index.en.md")

    for expected in (
        "一次构建 Agent，到处运行。",
        "Agent Runtime Platform",
        "assets/ksadk-runtime-platform-hero-wide.png",
        "真实 CLI 截图",
        "为什么需要 KsADK",
        "真实本地 Web UI 演示",
        "assets/ksadk-web-ui-screenshot.png",
        "assets/ksadk-local-debugging-demo.gif",
        "assets/ksadk-runtime-architecture.png",
        "生态定位",
        "VEADK",
        "AgentRun",
        "OpenTelemetry",
        "Hermes",
        "OpenClaw",
        "KSYUN_REGION=cn-beijing-6",
    ):
        assert expected in zh

    for expected in (
        "Build agents once. Run them anywhere.",
        "Agent Runtime Platform",
        "assets/ksadk-runtime-platform-hero-wide.png",
        "Real KsADK CLI screenshot",
        "Why KsADK",
        "real local Web UI",
        "assets/ksadk-web-ui-screenshot.png",
        "assets/ksadk-local-debugging-demo.gif",
        "assets/ksadk-runtime-architecture.png",
        "Ecosystem Positioning",
        "VEADK",
        "AgentRun",
        "OpenTelemetry",
        "Hermes",
        "OpenClaw",
        "KSYUN_REGION=cn-beijing-6",
    ):
        assert expected in en

    for text in (zh, en):
        assert "Agent Development Kit" not in text
        assert "成熟 Agent SDK" not in text


def test_docs_include_phase_one_positioning_pages():
    zh_pages = {
        "public-docs/getting-started/why-ksadk.md": (
            "为什么需要 KsADK",
            "一次构建 Agent，到处运行。",
            "ADK 解决 Agent 开发",
            "LangGraph",
            "OpenAI Agents SDK",
            "Agent Runtime Platform",
        ),
        "public-docs/getting-started/architecture.md": (
            "架构",
            "KsADK Agent Runtime Platform 架构",
            "Skill Runtime",
            "Workspace",
            "Sandbox",
            "Hermes / OpenClaw Runtime",
        ),
        "public-docs/getting-started/comparison.md": (
            "生态定位对比",
            "这页不是能力打分榜",
            "VEADK",
            "AgentRun",
            "repair_markdown(text, enabled=True)",
        ),
    }
    en_pages = {
        "public-docs/getting-started/why-ksadk.en.md": (
            "Why KsADK",
            "Build agents once. Run them anywhere.",
            "How do I run, debug, expose, deploy, and observe agents consistently?",
            "OpenAI Agents SDK",
            "Agent Runtime Platform",
        ),
        "public-docs/getting-started/architecture.en.md": (
            "Architecture",
            "KsADK Agent Runtime Platform architecture",
            "Skill Runtime",
            "Workspace",
            "Sandbox",
            "Hermes / OpenClaw Runtime",
        ),
        "public-docs/getting-started/comparison.en.md": (
            "Ecosystem Positioning",
            "not a feature scorecard",
            "VEADK",
            "AgentRun",
            "repair_markdown(text, enabled=True)",
        ),
    }

    for relative_path, expected_terms in {**zh_pages, **en_pages}.items():
        text = _read(relative_path)
        for expected in expected_terms:
            assert expected in text, f"{relative_path} missing {expected}"
        _assert_no_public_sensitive_patterns(relative_path, text)


def test_public_positioning_does_not_use_misleading_feature_scorecards():
    scorecard_headers = (
        "| 能力 | ADK | LangGraph | OpenAI Agents SDK | KsADK |",
        "| Capability | ADK | LangGraph | OpenAI Agents SDK | KsADK |",
    )
    misleading_cells = (
        "| OpenAI 兼容 API | 不内置 | 不内置 | 部分支持 | 支持 |",
        "| OpenAI Compatible API | No | No | Partial | Yes |",
    )

    for relative_path in (
        "README.md",
        "README.zh-CN.md",
        "README.en.md",
        "public-docs/index.md",
        "public-docs/index.en.md",
        "public-docs/getting-started/comparison.md",
        "public-docs/getting-started/comparison.en.md",
    ):
        text = _read(relative_path)
        for header in scorecard_headers:
            assert header not in text, f"{relative_path} still uses old scorecard header"
        for cell in misleading_cells:
            assert cell not in text, f"{relative_path} still uses misleading OpenAI comparison"

    for relative_path in (
        "public-docs/index.md",
        "public-docs/index.en.md",
        "public-docs/getting-started/comparison.md",
        "public-docs/getting-started/comparison.en.md",
    ):
        text = _read(relative_path)
        assert "VEADK" in text
        assert "AgentRun" in text


def test_readmes_stay_concise_and_do_not_duplicate_changelog():
    version_heading_pattern = re.compile(r"^##\s+0\.\d+\.\d+", re.MULTILINE)

    for relative_path in ("README.md", "README.zh-CN.md", "README.en.md"):
        text = _read(relative_path)
        assert not version_heading_pattern.search(text), (
            f"{relative_path} should link to CHANGELOG/Releases instead of listing version highlights"
        )
        assert "GitHub Releases" not in text
        assert "[CHANGELOG.md](CHANGELOG.md)" not in text
        assert "repair_markdown" not in text
        assert "最新" not in text
        assert len(text.splitlines()) < 200, f"{relative_path} should stay concise"
        assert '<h1 align="center">KsADK</h1>' in text
        assert '<p align="center">' in text
        assert 'width="860"' in text
        assert "ksadk-runtime-platform-hero-wide.png" in text


def test_public_positioning_uses_factual_ecosystem_focus_terms():
    expected_terms_by_path = {
        "public-docs/getting-started/comparison.md": ("A2UI/Frontend", "VeFaaS", "AgentRuntime 生命周期", "Serverless Devs"),
        "public-docs/getting-started/comparison.en.md": ("A2UI/Frontend", "VeFaaS", "AgentRuntime lifecycle", "Serverless Devs"),
    }

    for relative_path, expected_terms in expected_terms_by_path.items():
        text = _read(relative_path)
        for expected in expected_terms:
            assert expected in text, f"{relative_path} missing ecosystem evidence term: {expected}"


def test_public_visual_assets_are_present_and_nonempty():
    expected_assets = (
        "public-docs/assets/ksadk-runtime-platform-hero.png",
        "public-docs/assets/ksadk-runtime-platform-hero-wide.png",
        "public-docs/assets/ksadk-web-ui-screenshot.png",
        "public-docs/assets/ksadk-runtime-architecture.svg",
        "public-docs/assets/ksadk-runtime-architecture.png",
        "public-docs/assets/ksadk-local-debugging-demo.gif",
    )
    for relative_path in expected_assets:
        path = ROOT / relative_path
        assert path.is_file(), f"{relative_path} missing"
        assert path.stat().st_size > 4096, f"{relative_path} is unexpectedly small"


def test_readme_image_links_resolve_inside_repository():
    markdown_image = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    html_image = re.compile(r'<img\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)

    for relative_markdown_path in ("README.md", "README.zh-CN.md", "README.en.md"):
        text = _read(relative_markdown_path)
        image_targets = markdown_image.findall(text) + html_image.findall(text)
        assert image_targets, f"{relative_markdown_path} should contain rendered images"
        for image_target in image_targets:
            if "://" in image_target:
                continue
            image_path = (ROOT / image_target).resolve()
            assert image_path.is_relative_to(ROOT), (
                f"{relative_markdown_path} image escapes repository: {image_target}"
            )
            assert image_path.is_file(), (
                f"{relative_markdown_path} image target missing: {image_target}"
            )
            assert image_path.stat().st_size > 4096, (
                f"{relative_markdown_path} image target unexpectedly small: {image_target}"
            )


def test_public_navigation_is_task_oriented():
    mkdocs = _read("mkdocs.yml")
    for expected in ("Getting Started", "Build", "Run", "Deploy", "Observe", "Extend", "Reference"):
        assert expected in mkdocs
    for expected in (
        "为什么需要 KsADK: getting-started/why-ksadk.md",
        "架构: getting-started/architecture.md",
        "生态定位对比: getting-started/comparison.md",
        "为什么需要 KsADK: Why KsADK",
        "架构: Architecture",
        "生态定位对比: Ecosystem Positioning",
    ):
        assert expected in mkdocs
    assert "快速开始: Quick Start" in mkdocs
    assert "Kingsoft Cloud Agent Development Kit" not in mkdocs
    assert "金山云智能体开发套件" not in mkdocs


def test_markdown_repair_is_documented_as_opt_in():
    expected_by_path = {
        "public-docs/guides/agent-best-practices.md": (
            "Markdown 输出修复",
            "显式开启",
            "repair_markdown(text, enabled=True)",
            "默认不会改写模型原文",
        ),
        "public-docs/guides/agent-best-practices.en.md": (
            "Markdown Output Repair",
            "enable the lightweight repair helper",
            "repair_markdown(text, enabled=True)",
            "does not rewrite raw model output by default",
        ),
    }

    for relative_path, expected_terms in expected_by_path.items():
        text = _read(relative_path)
        for expected in expected_terms:
            assert expected in text, f"{relative_path} missing {expected}"


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
