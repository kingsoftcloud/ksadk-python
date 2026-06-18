from __future__ import annotations

import subprocess
from pathlib import Path

from ksadk.configs.env_registry import ENV_VAR_REGISTRY


def _source_ksadk_env_names() -> set[str]:
    result = subprocess.run(
        ["rg", "-o", "KSADK_[A-Z0-9_]+", "ksadk"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        line.rsplit(":", 1)[-1].strip()
        for line in result.stdout.splitlines()
        if line.strip()
    }


def test_env_registry_has_unique_sorted_names():
    names = [item.name for item in ENV_VAR_REGISTRY]

    assert len(names) == len(set(names))
    assert names == sorted(names)


def test_env_registry_covers_ksadk_env_vars_in_source():
    registry_names = {item.name for item in ENV_VAR_REGISTRY}

    assert _source_ksadk_env_names() <= registry_names


def test_env_registry_docs_cover_registered_names():
    doc_text = Path("docs/ksadk环境变量参考.md").read_text(encoding="utf-8")

    for item in ENV_VAR_REGISTRY:
        assert item.name in doc_text


def test_env_registry_defaults_ksadk_web_static_sync_to_latest_npm_release():
    specs = {item.name: item for item in ENV_VAR_REGISTRY}

    assert specs["KSADK_WEB_VERSION"].default == "latest"
    assert specs["KSADK_WEB_PACKAGE"].default == "@kingsoftcloud/ksadk-web"
    assert specs["KSADK_WEB_RELEASE_URL"].default == ""


def test_env_reference_documents_operational_metadata_and_common_runtime_vars():
    doc_text = Path("docs/ksadk环境变量参考.md").read_text(encoding="utf-8")

    for heading in ("是否必传", "别名/兼容", "配置方/来源", "是否业务自定义"):
        assert heading in doc_text

    for name in (
        "E2B_API_URL",
        "E2B_API_KEY",
        "SKILL_SPACE_ID",
        "AGENTENGINE_MODEL_ALLOWLIST",
        "AGENTENGINE_UI_DIR",
        "AGENT_BROWSER_EXECUTABLE_PATH",
        "AGENT_BROWSER_HOME",
        "FIRECRAWL_API_KEY",
        "HERMES_DASHBOARD_HOST",
        "HERMES_HOSTED_RUNTIME",
        "KSADK_KB_AMBIENT_POLICY",
        "KSADK_KB_SCHEME",
        "KSADK_LTM_AMBIENT_POLICY",
        "KSADK_MEMORY_BACKEND",
        "KDOCS_OPEN_BROWSER",
        "KS_ACCESS_KEY_ID",
        "KSYUN_ACCESS_KEY",
        "KSYUN_SECRET_KEY",
        "KSYUN_ACCOUNT_ID",
        "KSYUN_REGION",
        "MEM0_API_KEY",
        "OPENCLAW_ALLOWED_ORIGINS",
        "OPENCLAW_BROWSER_ENABLED",
        "OPENCLAW_DEFAULT_EXTENSIONS_DIR",
        "OPENCLAW_GATEWAY_INTERNAL_PORT",
        "OPENCLAW_GATEWAY_LOCAL_RESTART_MAX",
        "OPENCLAW_MODEL_CATALOG_JSON",
        "OPENCLAW_MODEL_API_KEY_SECRET_ID",
        "OPENCLAW_PRESET_SKILLS_DIR",
        "OPENCLAW_RUNTIME_PLAYWRIGHT_DOWNLOAD_HOST",
        "OPENCLAW_WEB_SAFE_SEARCH_MODE",
        "OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID",
        "OPENCLAW_WEB_FETCH_ENABLED",
        "COZE_WORKLOAD_IDENTITY_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL_NAME",
        "PLAYWRIGHT_DOWNLOAD_HOST",
    ):
        assert name in doc_text
