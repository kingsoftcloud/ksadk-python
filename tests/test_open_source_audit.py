from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "open_source_audit.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("open_source_audit", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_repo_audit_blocks_generated_and_internal_materials():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "public-repo",
        [
            "README.md",
            "ksadk/__init__.py",
            ".zread/wiki/current",
            "docs/internal/sandbox-runtime-design.md",
            "deploy/helm/ksadk-docs/values-pre.yaml",
            ".pypirc",
        ],
    )

    assert result.ok is False
    assert [violation.path for violation in result.violations] == [
        ".zread/wiki/current",
        "docs/internal/sandbox-runtime-design.md",
        "deploy/helm/ksadk-docs/values-pre.yaml",
        ".pypirc",
    ]
    assert result.counts["checked"] == 6
    assert result.counts["violations"] == 4


def test_public_repo_audit_blocks_non_curated_docs_tree():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "public-repo",
        [
            "docs/ksadk开源准备计划.md",
            "docs/release-checklist.md",
            "docs/ksadk-web-export-plan.md",
            "docs/open-source-final-blockers.md",
            "docs/open-source-requirements-traceability.md",
            "docs/open-source-review-packet.md",
            "docs/post-approval-commands.md",
            "docs/archive/kb-memory/memory_test_report.md",
            "docs/OpenClaw接口说明.md",
            "docs/superpowers/plans/2026-05-07-thinking-user-control-and-e2e-plan.md",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == ["non-curated-docs"] * 10


def test_public_repo_audit_blocks_internal_deploy_and_agent_ops_materials():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "public-repo",
        [
            "README.md",
            "deploy/hermes/Dockerfile",
            "deploy/openclaw/Dockerfile",
            "skills/agentengine-cluster-debug/SKILL.md",
            "examples/smart_assistant_adk/.env.example",
            "Makefile.openclaw",
            "Makefile.promo.Dockerfile",
            "CLAUDE.md",
            "AGENTS.md",
            ".env.example",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == [
        "internal-deploy-material",
        "internal-deploy-material",
        "internal-agent-ops-material",
        "non-curated-examples",
        "internal-deploy-material",
        "internal-deploy-material",
        "env-example",
    ]


def test_public_repo_audit_allows_curated_root_ai_guidance_files():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "public-repo",
        [
            "README.md",
            "README.en.md",
            "README.zh-CN.md",
            "AGENTS.md",
            "CLAUDE.md",
        ],
    )

    assert result.ok is True
    assert result.violations == []


def test_wheel_audit_blocks_hosted_ui_bundle_and_zread_snapshot():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "wheel",
        [
            "ksadk/server/static/index.html",
            "ksadk_runtime_common/schemas/event.json",
            "ksadk/server/web-ui/dist-hosted/index.html",
            ".zread/site/index.html",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == [
        "hosted-ui-bundle",
        "zread-output",
    ]


def test_github_pages_audit_allows_public_docs_but_blocks_zread_site():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "github-pages",
        [
            "index.html",
            "assets/search.js",
            "quickstart/index.html",
            ".zread/site/ksadk-docs/index.html",
            "docs/internal/index.html",
        ],
    )

    assert result.ok is False
    assert [violation.path for violation in result.violations] == [
        ".zread/site/ksadk-docs/index.html",
        "docs/internal/index.html",
    ]


def test_audit_paths_passes_for_public_wheel_file_list():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "wheel",
        [
            "ksadk/__init__.py",
            "ksadk/server/static/index.html",
            "ksadk/server/static/assets/app.js",
            "ksadk_runtime_common/schemas/event.json",
        ],
    )

    assert result.ok is True
    assert result.violations == []


def test_sdist_audit_blocks_hosted_ui_bundle():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "sdist",
        [
            "ksadk-0.6.1/README.md",
            "ksadk-0.6.1/ksadk/server/static/index.html",
            "ksadk-0.6.1/ksadk/server/web-ui/dist-hosted/index.html",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == ["hosted-ui-bundle"]


def test_sdist_audit_blocks_editable_web_ui_source():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "sdist",
        [
            "ksadk-0.6.1/README.md",
            "ksadk-0.6.1/ksadk/server/static/index.html",
            "ksadk-0.6.1/ksadk/server/web-ui/src/App.tsx",
            "ksadk-0.6.1/ksadk/server/web-ui/package.json",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == [
        "web-ui-source",
        "web-ui-source",
    ]


def test_audit_blocks_archive_root_prefixed_internal_paths():
    audit = _load_audit_module()

    result = audit.audit_paths(
        "sdist",
        [
            "ksadk-0.6.1/README.md",
            "ksadk-0.6.1/.zread/wiki/current",
            "ksadk-0.6.1/docs/internal/release.md",
            "ksadk-0.6.1/deploy/helm/ksadk-docs/values.yaml",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == [
        "zread-output",
        "internal-docs",
        "helm-values",
    ]


def test_git_files_excludes_deleted_paths(tmp_path):
    audit = _load_audit_module()

    (tmp_path / "keep.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "remove.txt").write_text("old\n", encoding="utf-8")
    audit.subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=audit.subprocess.PIPE)
    audit.subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    audit.subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        stdout=audit.subprocess.PIPE,
    )
    (tmp_path / "remove.txt").unlink()
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    assert audit.git_files(tmp_path) == ["keep.txt", "new.txt"]


def test_discover_files_falls_back_to_filesystem_for_non_git_directory(tmp_path):
    audit = _load_audit_module()
    (tmp_path / "README.md").write_text("ok\n", encoding="utf-8")
    (tmp_path / "src" / "App.tsx").parent.mkdir(parents=True)
    (tmp_path / "src" / "App.tsx").write_text("ok\n", encoding="utf-8")

    assert audit.discover_files(tmp_path) == ["README.md", "src/App.tsx"]


def test_content_audit_blocks_private_doc_domains_and_secret_shapes(tmp_path):
    audit = _load_audit_module()
    private_docs_url = "https://private-docs." + "example.invalid"
    aws_access_key_id = "AKIA" + "1234567890ABCDEF"
    openai_key = "sk-" + "A" * 48
    github_token = "ghp_" + "B" * 40

    (tmp_path / "README.md").write_text(
        f"Docs: {private_docs_url}\n", encoding="utf-8"
    )
    (tmp_path / "config.yml").write_text(f"AWS key {aws_access_key_id}\n", encoding="utf-8")
    (tmp_path / "llm.env").write_text(f"OPENAI_API_KEY={openai_key}\n", encoding="utf-8")
    (tmp_path / "repo.env").write_text(f"GITHUB_TOKEN={github_token}\n", encoding="utf-8")
    (tmp_path / "prod.env").write_text(
        "SECRET_KEY=prod_live_value_1234567890abcdef\n", encoding="utf-8"
    )
    (tmp_path / "tests.py").write_text(
        "SECRET_KEY=dummy-secret-value\nTOKEN=secret-token\n", encoding="utf-8"
    )
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00")

    result = audit.audit_file_contents(
        tmp_path,
        [
            "README.md",
            "config.yml",
            "llm.env",
            "repo.env",
            "prod.env",
            "tests.py",
            "binary.bin",
            "missing.txt",
        ],
    )

    assert result.ok is False
    assert [violation.rule for violation in result.violations] == [
        "private-doc-domain",
        "aws-access-key-id",
        "openai-api-key",
        "github-token",
        "long-lived-secret-assignment",
    ]


def test_content_audit_allows_aicp_internal_endpoints_but_blocks_other_internal_services(tmp_path):
    audit = _load_audit_module()
    (tmp_path / "aicp.py").write_text(
        "\n".join(
            [
                'AICP_PUBLIC = "aicp.api.ksyun.com"',
                'AICP_INTERNAL = "aicp.internal.api.ksyun.com"',
                'AICP_INNER = "aicp.inner.api.ksyun.com"',
            ]
        ),
        encoding="utf-8",
    )
    blocked_endpoint = "example." + "inner." + "api.ksyun.com"
    (tmp_path / "other.py").write_text(
        f'INTERNAL_ENDPOINT = "{blocked_endpoint}"\n',
        encoding="utf-8",
    )

    result = audit.audit_file_contents(tmp_path, ["aicp.py", "other.py"])

    assert result.ok is False
    assert [(violation.path, violation.rule) for violation in result.violations] == [
        ("other.py", "internal-service-endpoint")
    ]


def test_content_audit_allows_kspmas_internal_and_public_registry_paths(tmp_path):
    audit = _load_audit_module()
    (tmp_path / "settings.py").write_text(
        'KSPMAS_INTERNAL = "kspmas-internal.sdns.ksyun.com"\n',
        encoding="utf-8",
    )
    (tmp_path / "cmd_create.py").write_text(
        '# HERMES_IMAGE=hub.kce.ksyun.com/agentengine-public/hermes-agent:tag\n',
        encoding="utf-8",
    )
    blocked_registry = "hub.kce." + "ksyun.com/private-registry/image"
    (tmp_path / "other.py").write_text(
        f'IMAGE = "{blocked_registry}"\n',
        encoding="utf-8",
    )

    result = audit.audit_file_contents(
        tmp_path, ["settings.py", "cmd_create.py", "other.py"]
    )
    assert result.ok is False
    assert [(v.path, v.rule) for v in result.violations] == [
        ("other.py", "private-container-registry")
    ]


def test_release_artifact_root_audits_include_content_scan(tmp_path):
    audit = _load_audit_module()
    openai_key = "sk-" + "C" * 48

    (tmp_path / "ksadk-0.6.1" / "README.md").parent.mkdir(parents=True)
    (tmp_path / "ksadk-0.6.1" / "README.md").write_text(
        f"OPENAI_API_KEY={openai_key}\n", encoding="utf-8"
    )

    paths = audit.discover_files(tmp_path)
    for target in ("sdist", "wheel"):
        result = audit.merge_results(
            target,
            [
                audit.audit_paths(target, paths),
                audit.audit_file_contents(tmp_path, paths),
            ],
        )

        assert result.ok is False
        assert any(violation.rule == "openai-api-key" for violation in result.violations)


def test_ksadk_web_candidate_audit_blocks_consumer_shells_and_bad_metadata(tmp_path):
    audit = _load_audit_module()
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "web-ui",
                "private": True,
                "scripts": {
                    "build": "vite build && node scripts/sync-static.mjs",
                    "build:hosted": "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "export-manifest.json").write_text(
        json.dumps({"targetRepository": "https://example.invalid/ui"}),
        encoding="utf-8",
    )

    result = audit.merge_results(
        "ksadk-web-candidate",
        [
            audit.audit_paths(
                "ksadk-web-candidate",
                [
                    "package.json",
                    "export-manifest.json",
                    "Dockerfile",
                    "deploy/helm/values.yaml",
                    "tests/helm-contract.test.mjs",
                    "tests/sync-static.test.mjs",
                ],
            ),
            audit.audit_ksadk_web_candidate_metadata(
                tmp_path,
                ["package.json", "export-manifest.json"],
            ),
        ],
    )

    assert result.ok is False
    rules = [violation.rule for violation in result.violations]
    assert "hosted-deployment-shell" in rules
    assert "hosted-only-tests" in rules
    assert "ksadk-only-tests" in rules
    assert "wrong-ksadk-web-package-name" in rules
    assert "private-package" in rules
    assert "wrong-ksadk-web-build-script" in rules
    assert "consumer-sync-script-reference" in rules
    assert "wrong-ksadk-web-target-repository" in rules
    assert "wrong-ksadk-web-public-demo" in rules


def test_ksadk_web_candidate_audit_passes_generated_candidate(tmp_path):
    audit = _load_audit_module()
    generated_files = [
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/pull_request_template.md",
        ".github/workflows/ci.yml",
        ".github/workflows/pages.yml",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
    ]
    for path in generated_files:
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("ok\n", encoding="utf-8")

    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "@kingsoftcloud/ksadk-web",
                "version": "0.1.0",
                "license": "Apache-2.0",
                "homepage": "https://kingsoftcloud.github.io/ksadk-web/",
                "scripts": {
                    "build": "npm run build:ksadk",
                    "build:ksadk": "VITE_BASE_PATH=./ vite build --outDir dist-ksadk",
                    "build:hosted": "VITE_BASE_PATH=/chat/ vite build --outDir dist-hosted",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"name": "@kingsoftcloud/ksadk-web"}),
        encoding="utf-8",
    )
    (tmp_path / "export-manifest.json").write_text(
        json.dumps(
            {
                "targetRepository": "https://github.com/kingsoftcloud/ksadk-web",
                "publicDemo": "https://kingsoftcloud.github.io/ksadk-web/",
                "generatedCandidateFiles": generated_files,
            }
        ),
        encoding="utf-8",
    )
    paths = [
        *generated_files,
        "package.json",
        "package-lock.json",
        "export-manifest.json",
        "src/App.tsx",
        "tests/capabilities.test.mjs",
    ]

    result = audit.merge_results(
        "ksadk-web-candidate",
        [
            audit.audit_paths("ksadk-web-candidate", paths),
            audit.audit_file_contents(tmp_path, paths),
            audit.audit_ksadk_web_candidate_metadata(tmp_path, paths),
        ],
    )

    assert result.ok is True
    assert result.violations == []
