from __future__ import annotations

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
}
EN_DOC_URLS = {
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/quickstart/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/why-ksadk/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/architecture/",
    f"{DOCS_ROOT_URL}en/docs/framework/getting-started/comparison/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/observability-tracing/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/cloud-deployment/",
    f"{DOCS_ROOT_URL}en/docs/framework/guides/hosted-ui-events/",
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
                    candidate.relative_to(DOCS_CONTENT_ROOT).as_posix()
                    for candidate in candidates
                )
                broken.append(
                    f"{source.relative_to(DOCS_CONTENT_ROOT)} -> {target} ({display})"
                )

    assert not broken, "Broken internal documentation links:\n" + "\n".join(broken)


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

    assert pyproject["project"]["version"] == "0.8.0"
    assert 'VERSION = "0.8.0"' in version_text
    assert "Agent Runtime Platform" in pyproject["project"]["description"]
    assert "Agent Runtime Platform" in init_text
    assert "Agent Development Kit" not in pyproject["project"]["description"]
    assert "Agent Development Kit" not in init_text


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
    assert 'default: "0.3.0"' in workflow
    assert "approved_source_commit:" in workflow
    assert "Reviewed source commit SHA recorded in docs/maintainer-approval-record.md" in workflow
    assert "KSADK_WEB_VERSION: ${{ github.event.inputs.ksadk_web_version || '0.3.0' }}" in workflow
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
    assert 'KSADK_WEB_VERSION: "0.3.0"' in ci_workflow
    assert "PUBLIC_KSADK_WEB_VERSION" not in ci_workflow
    assert "KSADK_WEB_VERSION ?= 0.3.0" in makefile
    assert (
        "PUBLIC_TEST_TARGETS ?= tests/test_public_release_positioning.py "
        "tests/test_config_env_registry.py tests/test_managed_runtime_builder.py "
        "tests/test_managed_runtime_resolution.py tests/cli/test_cmd_create_codex.py "
        "tests/runners/test_codex_runner.py" in makefile
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
        "public-test docs-site-build public-build-check" in makefile
    )
    assert "NEXT_PUBLIC_BASE_PATH=/ksadk-python pnpm build:static" in makefile
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


def test_public_release_candidate_tracks_current_version():
    approval_record = _read("docs/maintainer-approval-record.md")

    assert "| Python package version | 0.8.0 |" in approval_record
    assert "make public-publish-check PUBLIC_PUBLISH_PHASE=pre-publish V=0.8.0" in approval_record


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
