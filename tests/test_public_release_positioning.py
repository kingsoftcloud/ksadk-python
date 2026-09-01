from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import tomllib

ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT_URL = "https://kingsoftcloud.github.io/ksadk-python/"
DOCS_CONTENT_ROOT = ROOT / "docs-site" / "content" / "docs"
ZH_DOC_URLS = {
    f"{DOCS_ROOT_URL}cn/docs/framework/getting-started/quickstart/",
    f"{DOCS_ROOT_URL}cn/docs/framework/getting-started/why-ksadk/",
    f"{DOCS_ROOT_URL}cn/docs/framework/getting-started/architecture/",
    f"{DOCS_ROOT_URL}cn/docs/framework/getting-started/comparison/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/observability-tracing/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/cloud-deployment/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/hosted-ui-events/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/agentkit-local-studio/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/plugins-and-automations/",
    f"{DOCS_ROOT_URL}cn/docs/framework/guides/runtime-architecture/",
    f"{DOCS_ROOT_URL}cn/docs/references/environment-variables/",
}
EN_DOC_URLS = {
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/quickstart/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/why-ksadk/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/architecture/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/comparison/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/observability-tracing/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/cloud-deployment/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/hosted-ui-events/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/agentkit-local-studio/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/plugins-and-automations/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/runtime-architecture/",
    f"{DOCS_ROOT_URL}en/docs/references/environment-variables/",
}

_DOCS_LINK_PATTERN = re.compile(
    r'(?:\[[^\]]+\]\(([^)\s]+)(?:\s+"[^"]*")?\)|'
    r'<Card\b[^>]*?\bhref=["\']([^"\']+)["\'])'
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _github_pages_urls(markdown: str) -> set[str]:
    return set(re.findall(r"https://kingsoftcloud\.github\.io/ksadk-python/[^>\s)\"]*", markdown))


def _docs_link_candidates(source: Path, target: str) -> tuple[Path, ...]:
    """Return source files that can render an internal Fumadocs link.

    The check deliberately covers Markdown links and ``<Card href>`` entries:
    static builds can render a page even when an in-content link would lead a
    reader to a 404.  External links and public assets are out of scope here.
    """

    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path or path.startswith(("http://", "https://", "mailto:", "/assets/")):
        return ()

    source_suffix = ".en.mdx" if source.name.endswith(".en.mdx") else ".mdx"
    if path.startswith("/"):
        segments = path.strip("/").split("/")
        if len(segments) < 2 or segments[:2] not in (["cn", "docs"], ["en", "docs"]):
            return ()
        locale_suffix = ".en.mdx" if segments[0] == "en" else ".mdx"
        relative = segments[2:]
        if relative == ["cli"]:
            return (DOCS_CONTENT_ROOT / "cli" / f"index{locale_suffix}",)
        base = DOCS_CONTENT_ROOT.joinpath(*relative)
        return (base.with_suffix(locale_suffix), base / f"index{locale_suffix}")

    base = source.parent / path
    if base.suffix:
        return (base,)
    return (base.with_suffix(source_suffix), base / f"index{source_suffix}")


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
    assert "0.8.0" not in readme
    assert "评审候选" not in readme


def test_public_readme_language_variants_keep_homepage_shape():
    root_readme = _read("README.md")
    zh_readme = _read("README.zh-CN.md")
    en_readme = _read("README.en.md")

    for text in (root_readme, zh_readme):
        assert "Kingsoft Cloud Agent Development Kit" in text
        assert "ksadk-runtime-platform-hero-wide.png" in text
        assert "ksadk-web-ui-screenshot.png" in text
        assert "ksadk-local-debugging-demo.gif" in text
        assert "ksadk-runtime-architecture.png" in text
        assert "发布版本：" not in text
        assert "## 0.6." not in text
        assert "0.8.0" not in text
        assert "评审候选" not in text

    assert "Kingsoft Cloud Agent Development Kit" in en_readme
    assert "ksadk-runtime-platform-hero-wide.png" in en_readme
    assert "ksadk-web-ui-screenshot.png" in en_readme
    assert "ksadk-local-debugging-demo.gif" in en_readme
    assert "ksadk-runtime-architecture.en.png" in en_readme
    assert "ksadk-runtime-architecture.png" not in en_readme
    assert "发布版本：" not in en_readme
    assert "## 0.6." not in en_readme
    assert "0.8.0" not in en_readme
    assert "Review Candidate" not in en_readme

    assert _github_pages_urls(root_readme) == {DOCS_ROOT_URL, *ZH_DOC_URLS}
    assert _github_pages_urls(zh_readme) == {DOCS_ROOT_URL, *ZH_DOC_URLS}
    assert _github_pages_urls(en_readme) == {DOCS_ROOT_URL, *EN_DOC_URLS}
    for stale_path in (
        "ksadk-python/getting-started/quickstart/",
        "ksadk-python/guides/observability-tracing/",
        "ksadk-python/en/getting-started/quickstart/",
        "ksadk-python/en/guides/observability-tracing/",
        "public-docs/assets/",
    ):
        assert stale_path not in root_readme
        assert stale_path not in zh_readme
        assert stale_path not in en_readme


def test_public_readme_docs_links_match_fumadocs_routes():
    checks = {
        "README.md": "cn",
        "README.zh-CN.md": "cn",
        "README.en.md": "en",
    }

    for readme_path, expected_locale in checks.items():
        text = _read(readme_path)
        urls = _github_pages_urls(text)
        docs_urls = [url for url in urls if "/docs/" in url]
        assert docs_urls, f"{readme_path} should link to Fumadocs pages"
        for url in docs_urls:
            path = urlparse(url).path.removeprefix("/ksadk-python/").strip("/")
            parts = path.split("/")
            assert parts[0] == expected_locale
            assert parts[1] == "docs"
            doc_segments = parts[2:]
            suffix = ".en.mdx" if expected_locale == "en" else ".mdx"
            candidate = DOCS_CONTENT_ROOT.joinpath(*doc_segments).with_suffix(suffix)
            index_candidate = DOCS_CONTENT_ROOT.joinpath(*doc_segments, f"index{suffix}")
            assert candidate.exists() or index_candidate.exists(), url


def test_docs_internal_links_resolve_to_rendered_pages():
    broken: list[str] = []
    for source in sorted(DOCS_CONTENT_ROOT.rglob("*.mdx")):
        text = source.read_text(encoding="utf-8")
        for match in _DOCS_LINK_PATTERN.finditer(text):
            target = next(value for value in match.groups() if value is not None)
            candidates = _docs_link_candidates(source, target)
            if candidates and not any(candidate.exists() for candidate in candidates):
                display = " or ".join(
                    candidate.relative_to(DOCS_CONTENT_ROOT).as_posix() for candidate in candidates
                )
                broken.append(f"{source.relative_to(DOCS_CONTENT_ROOT)} -> {target} ({display})")

    assert not broken, "Broken internal documentation links:\n" + "\n".join(broken)


def test_docs_relative_links_are_resolvable_by_fumadocs():
    """Relative docs links must use source paths that createRelativeLink understands.

    The static site uses trailing-slash routes.  An unresolved link such as
    ``managed-runtime`` is emitted unchanged and the browser resolves it below
    the current page (``.../agentkit-local-studio/managed-runtime``), which is a
    404 even though the sibling page exists.  Fumadocs resolves locale-aware
    links only when they start with ``./`` or ``../`` and name the MDX source.
    """

    unsafe: list[str] = []
    for source in sorted(DOCS_CONTENT_ROOT.rglob("*.mdx")):
        text = source.read_text(encoding="utf-8")
        for match in _DOCS_LINK_PATTERN.finditer(text):
            target = next(value for value in match.groups() if value is not None)
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path or path.startswith(("http://", "https://", "mailto:", "/")):
                continue
            if Path(path).suffix and not path.endswith(".mdx"):
                continue
            if not path.startswith(("./", "../")) or not path.endswith(".mdx"):
                unsafe.append(f"{source.relative_to(DOCS_CONTENT_ROOT)} -> {target}")

    assert not unsafe, "Relative links that Fumadocs will emit unresolved:\n" + "\n".join(unsafe)


def test_docs_content_and_navigation_have_complete_english_variants():
    chinese_pages = {
        path.relative_to(DOCS_CONTENT_ROOT).as_posix()
        for path in DOCS_CONTENT_ROOT.rglob("*.mdx")
        if not path.name.endswith(".en.mdx")
    }
    english_pages = {
        path.relative_to(DOCS_CONTENT_ROOT).as_posix().replace(".en.mdx", ".mdx")
        for path in DOCS_CONTENT_ROOT.rglob("*.en.mdx")
    }
    assert chinese_pages == english_pages

    chinese_meta = sorted(DOCS_CONTENT_ROOT.rglob("meta.json"))
    missing_meta = [
        path.relative_to(DOCS_CONTENT_ROOT).as_posix()
        for path in chinese_meta
        if not path.with_name("meta.en.json").exists()
    ]
    assert not missing_meta, "Missing English navigation metadata:\n" + "\n".join(missing_meta)

    def navigation_identity(entry: str) -> str:
        if entry.startswith("---"):
            icon = re.match(r"---(?:\[([^]]+)\])?", entry)
            return f"separator:{icon.group(1) if icon else ''}"
        return entry

    for chinese_path in chinese_meta:
        english_path = chinese_path.with_name("meta.en.json")
        chinese = json.loads(chinese_path.read_text(encoding="utf-8"))
        english = json.loads(english_path.read_text(encoding="utf-8"))
        assert [navigation_identity(item) for item in chinese.get("pages", [])] == [
            navigation_identity(item) for item in english.get("pages", [])
        ], chinese_path.relative_to(DOCS_CONTENT_ROOT)

    i18n_config = _read("docs-site/lib/i18n.ts")
    assert "fallbackLanguage: null" in i18n_config


def test_docs_navigation_exposes_the_083_user_journeys():
    chinese = _read("docs-site/content/docs/framework/meta.json")
    english = _read("docs-site/content/docs/framework/meta.en.json")
    for expected in (
        "Studio 与本地开发",
        "Harness 与插件化",
        "统一事件与互操作",
        "构建与部署",
        "运维与维护",
    ):
        assert expected in chinese
    for expected in (
        "Studio and Local Development",
        "Harness and Plugins",
        "Events and Interoperability",
        "Build and Deploy",
        "Operations and Maintenance",
    ):
        assert expected in english

    page_order = json.loads(chinese)["pages"]
    assert page_order.index("guides/agentkit-local-studio") < page_order.index(
        "guides/runtime-architecture"
    )
    assert page_order.index("guides/runtime-architecture") < page_order.index(
        "guides/plugins-and-automations"
    )
    assert page_order.index("guides/plugins-and-automations") < page_order.index(
        "guides/hosted-ui-events"
    )

    landing = _read("docs-site/content/docs/framework/index.mdx")
    assert "Harness 与插件化" in landing
    assert "/cn/docs/framework/guides/runtime-architecture" in landing
    assert "/cn/docs/framework/guides/plugins-and-automations" in landing


def test_docs_versioned_facts_match_083_source():
    makefile = _read("Makefile")
    web_version_match = re.search(r"^KSADK_WEB_VERSION \?= (\S+)$", makefile, re.MULTILINE)
    assert web_version_match is not None
    web_version = web_version_match.group(1)
    assert web_version == "0.3.4"

    versioned_docs = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(DOCS_CONTENT_ROOT.rglob("*.mdx"))
    )
    public_surfaces = "\n".join(
        (
            versioned_docs,
            _read("README.md"),
            _read("README.zh-CN.md"),
            _read("README.en.md"),
            _read("docs-site/app/[lang]/(home)/page.tsx"),
        )
    )
    for stale in (
        "KSADK_WEB_VERSION=0.3.2",
        "`KSADK_WEB_VERSION` | `0.3.2`",
        "0.8.2 default is `0.3.2`",
        "0.8.2 默认 `0.3.2`",
        "V=0.6.7",
        "`0.8.0` is still a release candidate",
        "`0.8.0` 仍是候选版本",
        "RuntimeEvent v1",
    ):
        assert stale not in public_surfaces

    assert 'version = "0.8.3"' in _read("pyproject.toml")
    assert "0.8.3" in _read("docs-site/app/[lang]/(home)/page.tsx")

    for relative in (
        "framework/guides/web-ui-source.mdx",
        "framework/guides/web-ui-source.en.mdx",
        "references/environment-variables.mdx",
        "references/environment-variables.en.mdx",
    ):
        assert f"`{web_version}`" in _read(f"docs-site/content/docs/{relative}")

    a2a_dependency = re.search(r'"a2a-sdk\[fastapi\]==([^"]+)"', _read("pyproject.toml"))
    assert a2a_dependency is not None
    for relative in (
        "framework/guides/a2a-runtime.mdx",
        "framework/guides/a2a-runtime.en.mdx",
    ):
        assert f"a2a-sdk=={a2a_dependency.group(1)}" in _read(
            f"docs-site/content/docs/{relative}"
        )


def test_documented_cli_snippets_only_use_registered_top_level_commands():
    from ksadk.cli import _register_commands, cli

    _register_commands()

    documented: set[str] = set()
    fence_pattern = re.compile(r"```(?:bash|shell|console)[^\n]*\n(.*?)```", re.DOTALL)
    command_pattern = re.compile(
        r"^(?:\$\s+)?(?:(?:uv run|python -m)\s+)?(?:agentengine|ksadk)\s+([a-z][a-z0-9-]*)",
        re.MULTILINE,
    )
    for source in DOCS_CONTENT_ROOT.rglob("*.mdx"):
        for block in fence_pattern.findall(source.read_text(encoding="utf-8")):
            documented.update(command_pattern.findall(block))

    registered = set(cli.commands)
    assert documented <= registered, sorted(documented - registered)
    for required in ("init", "run", "web", "studio", "plugin", "eval", "deploy"):
        assert required in documented


def test_083_runtime_configuration_is_present_in_the_public_reference():
    chinese = _read("docs-site/content/docs/references/environment-variables.mdx")
    english = _read("docs-site/content/docs/references/environment-variables.en.mdx")
    for name in (
        "KSADK_AGENT_KERNEL",
        "KSADK_A2A_CONTROL_PLANE_URL",
        "KSADK_A2UI_GENERATION_TIMEOUT_SECONDS",
        "KSADK_DSH_BIN",
        "KSADK_DSH_HOME",
        "KSADK_DSH_PROFILE",
        "KSADK_STUDIO_SESSION_TOKEN",
        "KSADK_WEB_VERSION",
    ):
        assert name in chinese
        assert name in english

    for text in (chinese, english):
        assert "agentengine.yaml" in text or "AGENTENGINE_MANAGED_RUNTIME_NAME" in text


def test_docs_site_cloud_deployment_guides_and_static_search_are_publicly_reachable():
    docs_root = ROOT / "docs-site"
    search = _read("docs-site/components/search.tsx")

    assert "from: assetPath('/api/search')" in search
    assert "import { assetPath } from '@/lib/shared'" in search
    assert (docs_root / "content/docs/framework/guides/cloud-deployment.mdx").exists()
    assert (docs_root / "content/docs/framework/guides/cloud-deployment.en.mdx").exists()
    assert '"guides/cloud-deployment"' in _read("docs-site/content/docs/framework/meta.json")


def test_legacy_deploy_and_examples_are_not_tracked_in_source_repo():
    if not (ROOT / ".git").exists():
        return

    tracked = subprocess.run(
        ["git", "ls-files", "deploy", "examples"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    makefile = _read("Makefile")

    assert tracked == ""
    assert "-f deploy/hermes/Dockerfile" not in makefile
    assert "-f deploy/openclaw/Dockerfile" not in makefile
    assert "-f deploy/openclaw-user-template/Dockerfile" not in makefile


def test_public_metadata_uses_runtime_platform_positioning():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    init_text = _read("ksadk/__init__.py")
    version_text = _read("ksadk/version.py")
    changelog = _read("CHANGELOG.md")

    assert pyproject["project"]["version"] == "0.8.3"
    assert 'VERSION = "0.8.3"' in version_text
    assert "## [0.8.3] - Unreleased" in changelog
    assert "## [0.8.1] - 2026-08-10" in changelog
    assert "`langchain-openai` 仅随" in changelog
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


def test_runtime_event_v2_capability_is_documented_publicly():
    changelog = _read("CHANGELOG.md")
    readme = _read("README.md")
    zh_readme = _read("README.zh-CN.md")
    en_readme = _read("README.en.md")

    # The canonical-v2 / read-only-v1 boundary replaces the stale additive-v1 claim.
    assert "schema_version=2" in changelog
    assert "只读兼容投影" in changelog
    assert "继续保持 v1 additive 兼容" not in changelog

    for text in (readme, zh_readme):
        assert "RuntimeEvent schema v2 契约" in text
        assert "RuntimeEventVersions=[1,2]" in text
        assert "RuntimeEventDefault=2" in text
        assert 'RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]' in text
        assert 'RuntimeEventV1ProjectionDefault="snapshot_only"' in text

    assert "RuntimeEvent Schema v2 Contract" in en_readme
    assert "RuntimeEventVersions=[1,2]" in en_readme
    assert "RuntimeEventDefault=2" in en_readme
    assert 'RuntimeEventV1ProjectionModes=["snapshot_only","identity_replace"]' in en_readme
    assert 'RuntimeEventV1ProjectionDefault="snapshot_only"' in en_readme


def test_adk_extra_avoids_litellm_source_build_on_windows_python_3_13():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    adk_requirements = pyproject["project"]["optional-dependencies"]["adk"]

    assert (
        "litellm>=1.0.0; platform_system != 'Windows' or python_version < '3.13'"
        in adk_requirements
    )


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
    assert "publish_target:" in workflow
    assert "alias-only" in workflow
    assert 'default: "0.3.4"' in workflow
    assert "approved_source_commit:" in workflow
    assert "Reviewed source commit SHA recorded in docs/maintainer-approval-record.md" in workflow
    assert "KSADK_WEB_VERSION: ${{ github.event.inputs.ksadk_web_version || '0.3.4' }}" in workflow
    assert (
        "KSADK_APPROVED_SOURCE_COMMIT: "
        "${{ github.event.inputs.approved_source_commit || "
        "vars.KSADK_APPROVED_SOURCE_COMMIT }}" in workflow
    )
    assert "make sync-ksadk-web-static" in workflow
    assert "make public-preflight" in workflow
    assert "make public-audit public-test public-build-alias-check" in workflow
    assert "make public-publish-gate" in workflow
    assert "if: env.PUBLISH_TARGET == 'full'" in workflow
    assert "Publish alias package to PyPI" in workflow
    assert "packages-dir: dist-alias" in workflow
    assert "github.event.inputs.publish_target != 'alias-only'" in workflow
    assert "make open-source-audit-dist" in ci_workflow
    assert "make public-test" in ci_workflow
    assert "tests/test_conversation_runtime.py" not in ci_workflow
    assert "tests/test_server_session_app.py" not in ci_workflow
    assert 'KSADK_WEB_VERSION: "0.3.4"' in ci_workflow
    assert "PUBLIC_KSADK_WEB_VERSION" not in ci_workflow
    assert "KSADK_WEB_VERSION ?= 0.3.4" in makefile
    assert (
        "PUBLIC_TEST_TARGETS ?= tests/test_public_release_positioning.py "
        "tests/test_docs_site_output_audit.py tests/test_config_env_registry.py "
        "tests/test_managed_runtime_builder.py "
        "tests/test_managed_runtime_resolution.py tests/cli/test_cmd_create_codex.py "
        "tests/runners/test_adapter_contract.py" in makefile
    )
    assert "public-sync-ksadk-web-static: sync-ksadk-web-static" in makefile
    assert "python3 scripts/open_source_audit.py --target public-repo" in makefile
    assert "open-source-audit-dist:" in makefile
    assert "public-release-approval-check:" in makefile
    assert "public-publish-gate: public-release-approval-check" in makefile
    assert "scripts/check_approval_record.py" in makefile
    assert '--expected-current-commit "$${KSADK_APPROVED_SOURCE_COMMIT:-}"' not in makefile
    assert "KSADK_APPROVED_SOURCE_COMMIT is required" in makefile
    assert "public-build-check: clean-dist sync-ksadk-web-static" in makefile
    assert "verify-ksadk-web-wheel-static" in makefile
    assert (
        "public-preflight: public-version-gate public-audit sync-ksadk-web-static "
        "public-test docs-site-build phase2-release-preflight" in makefile
    )
    assert "NEXT_PUBLIC_BASE_PATH=/ksadk-python pnpm build:static" in makefile
    assert "scripts/audit_docs_site_output.py" in makefile
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_ksadk_web_npm_consumers_use_the_configured_registry():
    makefile = _read("Makefile")
    registry = "https://registry.example.test/npm"

    assert 'KSADK_WEB_NPM := npm --registry="$(KSADK_WEB_REGISTRY)"' in makefile
    assert (
        '$(KSADK_WEB_NPM) pack "$(KSADK_WEB_PACKAGE)@$(patsubst v%,%,$(KSADK_WEB_VERSION))"'
        in makefile
    )
    assert "$(KSADK_WEB_NPM) --prefix ksadk/studio/react-ui ci" in makefile
    assert '$(KSADK_WEB_NPM) --prefix "$(STUDIO_REACT_DIR)" ci' in makefile
    assert (
        "REGISTRY_JSON=$$(curl -fsSL "
        '"$(KSADK_WEB_REGISTRY)/$(KSADK_WEB_PACKAGE)/$(KSADK_WEB_VERSION)")' in makefile
    )
    assert 'npm pack "$(KSADK_WEB_PACKAGE)@$(patsubst v%,%,$(KSADK_WEB_VERSION))"' not in makefile
    assert "KSADK_WEB_VERSION ?= 0.3.4" in makefile

    sync_dry_run = subprocess.run(
        [
            "make",
            "-n",
            "sync-ksadk-web-static",
            f"KSADK_WEB_REGISTRY={registry}",
            "KSADK_WEB_CACHE_DIR=/tmp/ksadk-web-registry-contract",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert f'npm --registry="{registry}" pack "@kingsoftcloud/ksadk-web@0.3.4"' in sync_dry_run
    assert f'curl -fsSL "{registry}/@kingsoftcloud/ksadk-web/0.3.4"' in sync_dry_run

    studio_dry_run = subprocess.run(
        ["make", "-n", "build-studio-static", f"KSADK_WEB_REGISTRY={registry}"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert f'npm --registry="{registry}" --prefix "ksadk/studio/react-ui" ci' in studio_dry_run


def test_public_ci_runs_gitleaks_and_documents_branch_protection():
    ci_workflow = _read(".github/workflows/ci.yml")
    secret_workflow = _read(".github/workflows/secret-patterns.yml")
    branch_protection = _read(".github/BRANCH_PROTECTION.md")
    approval_record = _read("docs/maintainer-approval-record.md")

    assert 'GITLEAKS_VERSION: "8.28.0"' in secret_workflow
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in secret_workflow
    assert "/tmp/gitleaks detect --source ." in secret_workflow
    assert "fetch-depth: 0" in secret_workflow
    for workflow in (ci_workflow, secret_workflow):
        assert "if [ -f export-manifest.json ]" in workflow
        assert "scripts/prepare_ksadk_python_export.py" in workflow
        assert '--root "$audit_root"' in workflow
        assert "--target public-repo" in workflow
    assert "Require a pull request before merging" in branch_protection
    assert "CI / test" in branch_protection
    assert "Secret Pattern Audit / scan" in branch_protection
    assert "CodeQL / analyze" in branch_protection
    assert "pypi environment" in branch_protection
    assert "Branch protection and publish environment are configured" in approval_record


def test_public_release_candidate_tracks_current_version():
    approval_record = _read("docs/maintainer-approval-record.md")

    assert "| Python package version | 0.8.3 |" in approval_record
    assert "make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.3" in approval_record


def test_0_8_changelog_is_ready_for_authorized_release():
    changelog = _read("CHANGELOG.md")
    release_section = changelog.split("## [0.8.0]", 1)[1].split("## [0.7.0]", 1)[0]

    assert "## [0.8.0] - 2026-07-29" in changelog
    assert "Review candidate" not in release_section
    assert "本候选" not in release_section
    assert "本条目不构成发布批准" not in release_section
    assert "a76f2de7565ffe34d44a9d17257401fa805de0de" in release_section
    assert "@kingsoftcloud/ksadk-web@0.3.0" in release_section
    assert "Codex ManagedRuntime" in release_section


def test_0_8_1_changelog_pins_the_compatible_ksadk_web_release():
    changelog = _read("CHANGELOG.md")
    release_section = changelog.split("## [0.8.1]", 1)[1].split("## [0.8.0]", 1)[0]

    assert "## [0.8.1] - 2026-08-10" in changelog
    assert "@kingsoftcloud/ksadk-web@0.3.1" in release_section


def test_public_release_sync_compares_exported_file_contents():
    workflow = _read("docs/public-release-workflow.md")

    assert "rsync -a --checksum --delete --exclude .git" in workflow


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
        web_ui_files = "\n".join(
            str(path.relative_to(ROOT))
            for path in (ROOT / "ksadk/server/web-ui").glob("**/*")
            if path.is_file()
        )

    assert "ksadk/server/static/**" in gitignore
    assert "ksadk/server/web-ui/" in gitignore
    assert '"server/static/**/*"' in pyproject
    assert "server/web-ui" not in pyproject
    assert web_ui_files == ""


def test_compiled_studio_asset_tracking_matches_release_boundary():
    gitignore = _read(".gitignore")
    pyproject = _read("pyproject.toml")
    editable_source = (ROOT / "ksadk/studio/react-ui/package.json").is_file()
    if (ROOT / ".git").exists():
        tracked_static_files = subprocess.run(
            ["git", "ls-files", "ksadk/studio/static/**"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.splitlines()
    else:
        tracked_static_files = []

    assert "ksadk/studio/static/**" in gitignore
    assert '"studio/static/**/*"' in pyproject
    if editable_source:
        assert tracked_static_files == []
    else:
        assert (ROOT / "ksadk/studio/static/index.html").is_file()
        if (ROOT / ".git").exists():
            assert tracked_static_files


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
