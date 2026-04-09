"""
agentengine openclaw - OpenClaw 资源管理

设计目标:
- 和 Agent 部署完全一致，复用 CreateAgentProduct 接口 (Container 模式)
- Framework 标记为 "openclaw"，区分于普通 Agent
- 预构建公共镜像，用户无需自行构建
- 模型配置通过 EnvironmentVariables 传递，自动复用 OPENAI_* 变量
"""

from __future__ import annotations

import copy
import io
import os
import asyncio
import json
import hashlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import click
import requests
from rich.measure import Measurement
from rich.table import Table as RichTable

from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import resolve_openclaw_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error
from ksadk.cli.resource_common import ResourceActionDescriptor
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_resource_group_help,
    confirm_destructive,
    confirm_options,
    pagination_options,
    print_next_action_hint,
    render_descriptor_list,
    render_descriptor_status,
    region_option,
)
from ksadk.cli.ui import (
    emit_json,
    get_console,
    is_json_output,
    is_stdout_tty,
    json_dumps,
    output_option as cli_output_option,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
    status_rich_style,
)
from ksadk.openclaw_gateway import OpenClawGatewayClient, OpenClawGatewayError

console = get_console()
# 默认 OpenClaw 镜像 (KCR 个人版)
DEFAULT_OPENCLAW_NAMESPACE = "agentengine-public"
DEFAULT_OPENCLAW_REPO = "openclaw"
DEFAULT_OPENCLAW_VERSION = "latest"
DEFAULT_OPENCLAW_REGISTRY = "hub.kce.ksyun.com"
DEFAULT_OPENCLAW_NAME = "openclaw-gateway"
DEFAULT_TRUSTED_PROXY_USER_HEADER = "x-forwarded-user"
DEFAULT_TRUSTED_PROXY_CIDRS = [
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "35.0.0.0/8",
]
_GLOBAL_ENV_CACHE: Optional[Dict[str, str]] = None
OPENCLAW_SECURITY_PROFILES = ("relaxed", "strict", "strictest")
OPENCLAW_CHANNELS = ("weixin", "feishu", "agentspace")
WEIXIN_PLUGIN_ID = "openclaw-weixin"
FEISHU_PLUGIN_ID = "openclaw-lark"
FEISHU_CHANNEL_KEY = "feishu"
AGENTSPACE_PLUGIN_ID = "agentspace"
AGENTSPACE_CHANNEL_KEY = "agentspace"
AGENTSPACE_DEFAULT_ACCOUNT_ID = "default"
AGENTSPACE_DEFAULT_KEY_SOURCE = "openclaw_agentspace"
AGENTSPACE_LOGIN_URL_API = "https://agentspace.wps.cn/v7/devhub/users/login_url"
AGENTSPACE_USER_TOKEN_API = "https://agentspace.wps.cn/v7/devhub/users/user_token"
AGENTSPACE_CURRENT_USER_API = "https://agentspace.wps.cn/v7/devhub/users/current"
AGENTSPACE_SKILL_NAME = "wps365-skill"
OPENCLAW_CHANNEL_SPECS = {
    "weixin": {
        "plugin_id": WEIXIN_PLUGIN_ID,
        "channel_key": WEIXIN_PLUGIN_ID,
        "default_account_id": "default",
    },
    "feishu": {
        "plugin_id": FEISHU_PLUGIN_ID,
        "channel_key": FEISHU_CHANNEL_KEY,
        "default_account_id": "default",
    },
    "agentspace": {
        "plugin_id": AGENTSPACE_PLUGIN_ID,
        "channel_key": AGENTSPACE_CHANNEL_KEY,
        "default_account_id": AGENTSPACE_DEFAULT_ACCOUNT_ID,
    },
}
OPENCLAW_GATEWAY_READY_STATUSES = {"RUNNING", "READY", "HEALTHY"}
OPENCLAW_GATEWAY_BLOCKED_STATUSES = {
    "DELETED",
    "DELETING",
    "ERROR",
    "FAILED",
    "STOPPED",
    "STOPPING",
    "TERMINATED",
    "TERMINATING",
}

OPENCLAW_RESOURCE = ResourceDescriptor(
    name="OpenClaw",
    summary="OpenClaw 资源管理。",
    resource_key="openclaw",
    actions=(
        ResourceActionDescriptor(
            name="deploy",
            canonical_command="agentengine openclaw deploy",
            help_text="部署 OpenClaw 到云端",
            kind="write",
            supports_output=True,
            supports_dry_run=True,
        ),
        ResourceActionDescriptor(
            name="list",
            canonical_command="agentengine openclaw list",
            help_text="列出已部署的 OpenClaw",
        ),
        ResourceActionDescriptor(
            name="status",
            canonical_command="agentengine openclaw status [openclaw_ref]",
            help_text="查看单个 OpenClaw 状态",
        ),
        ResourceActionDescriptor(
            name="gateway",
            canonical_command="agentengine openclaw gateway",
            help_text="Gateway 入口、日志与诊断",
            kind="interactive",
        ),
        ResourceActionDescriptor(
            name="channel",
            canonical_command="agentengine openclaw channel",
            help_text="Channel 统一入口",
            kind="interactive",
        ),
        ResourceActionDescriptor(
            name="delete",
            canonical_command="agentengine openclaw delete [openclaw_ref...]",
            help_text="删除一个或多个 OpenClaw",
            kind="destructive",
            supports_output=True,
            supports_dry_run=True,
            supports_yes=True,
        ),
    ),
    list_schema=ResourceListSchema(
        title="OpenClaw 列表",
        noun="OpenClaw",
        columns=(
            {"header": "ID", "key": "id", "style": "#58a6ff", "no_wrap": True},
            {"header": "名称", "key": "name", "style": "white"},
            {"header": "状态", "key": "status", "no_wrap": True, "justify": "center"},
            {"header": "Endpoint", "key": "endpoint", "style": "#8b949e", "overflow": "ellipsis"},
            {"header": "区域", "key": "region", "style": "#8b949e"},
        ),
        empty_message="没有找到已部署的 OpenClaw",
    ),
    status_schema=ResourceStatusSchema(
        title="OpenClaw 状态",
        next_steps=("agentengine openclaw list",),
    ),
    examples=(
        "agentengine openclaw deploy",
        "agentengine openclaw list",
        "agentengine openclaw status <id>",
        "agentengine openclaw gateway open <id>",
        "agentengine openclaw channel status <id> --probe",
        "agentengine openclaw channel connect <id> --channel weixin",
        "agentengine openclaw delete <id>",
    ),
    missing_ref_message="请指定 OpenClaw ID/名称，或在 OpenClaw 项目目录下运行",
    resolution_commands=("agentengine openclaw list",),
)


def _abort_openclaw_error(
    err: Exception,
    *,
    context: str | None = None,
    argv: list[str] | None = None,
) -> None:
    abort_with_cli_error(err, context=context, argv=argv)


def _generate_default_openclaw_name(prefix: str = DEFAULT_OPENCLAW_NAME) -> str:
    """生成低碰撞默认名称。

    格式: openclaw-gateway-MMDDHHMMSS-xxxxxx
    - 时间粒度提升到秒
    - 追加 6 位十六进制随机后缀（24-bit）
    """
    ts = datetime.now().strftime("%m%d%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{prefix}-{ts}-{suffix}"


def _get_global_env() -> Dict[str, str]:
    """读取全局配置并转换为环境变量字典（带进程级缓存）。"""
    global _GLOBAL_ENV_CACHE
    if _GLOBAL_ENV_CACHE is not None:
        return _GLOBAL_ENV_CACHE

    try:
        from ksadk.configs.global_config import get_env_from_global_config
        _GLOBAL_ENV_CACHE = {
            str(k): str(v).strip()
            for k, v in get_env_from_global_config().items()
            if k and v is not None and str(v).strip() != ""
        }
    except Exception:
        _GLOBAL_ENV_CACHE = {}

    return _GLOBAL_ENV_CACHE


def _resolve_env(*keys: str, default: Optional[str] = None) -> Optional[str]:
    """按优先级从环境变量和全局配置中获取值。"""
    for key in keys:
        val = os.getenv(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    global_env = _get_global_env()
    for key in keys:
        val = global_env.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def _resolve_model_base_url(cli_value: Optional[str]) -> Optional[str]:
    """解析模型 Base URL，缺失时回退到 settings.model.api_base（KSPMAS 自动探测）。"""
    if cli_value and str(cli_value).strip():
        return str(cli_value).strip()

    from_env = _resolve_env(
        "OPENCLAW_MODEL_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "LLM_API_BASE",
        "MODEL_API_BASE",
    )
    if from_env:
        return from_env

    try:
        from ksadk.configs.settings import settings
        api_base = settings.model.api_base
        if api_base and str(api_base).strip():
            return str(api_base).strip()
    except Exception:
        pass

    return None


def _summarize_openclaw_account(agents: list[Dict[str, Any]]) -> str:
    """汇总列表所属账号，优先使用响应字段，缺失时回退当前 CLI 上下文。"""
    accounts = sorted(
        {
            str(item.get("account_id") or "").strip()
            for item in agents
            if str(item.get("account_id") or "").strip()
        }
    )
    if accounts:
        return ",".join(accounts)
    return _resolve_env("KSYUN_ACCOUNT_ID", default="-") or "-"


def _summarize_openclaw_region(agents: list[Dict[str, Any]], fallback_region: Optional[str]) -> str:
    """汇总列表中的 region，缺失时回退命令参数。"""
    regions = sorted(
        {
            str(item.get("region") or "").strip()
            for item in agents
            if str(item.get("region") or "").strip()
        }
    )
    if regions:
        return ",".join(regions)
    return str(fallback_region or "-")


def _print_openclaw_list_summary(table: RichTable, summary_text: str) -> None:
    """将摘要贴在表格下方；宽度不足时退化成普通单行。"""
    table_width = Measurement.get(console, console.options, table).maximum
    summary_width = Measurement.get(console, console.options, summary_text).maximum
    if table_width >= summary_width:
        summary_grid = RichTable.grid(expand=False)
        summary_grid.add_column(justify="right", width=table_width)
        summary_grid.add_row(f"[muted]{summary_text}[/]")
        console.print(summary_grid)
        return
    console.print(f"[muted]{summary_text}[/]")


def _normalize_ui_locale(raw: Optional[str]) -> str:
    """标准化 UI 语言代码，默认 zh-CN。"""
    text = str(raw or "").strip()
    if not text:
        return "zh-CN"

    base = text.split(".", 1)[0].replace("_", "-").strip()
    low = base.lower()

    if low in {"c", "c-utf-8", "c.utf-8", "posix"}:
        return "zh-CN"
    if low.startswith("zh-tw") or low.startswith("zh-hk") or low.startswith("zh-mo") or low.startswith("zh-hant"):
        return "zh-TW"
    if low.startswith("zh"):
        return "zh-CN"
    if low.startswith("pt"):
        return "pt-BR"
    if low.startswith("de"):
        return "de"
    if low.startswith("en"):
        return "en"

    return "zh-CN"


def _is_truthy(raw: Optional[str]) -> bool:
    text = str(raw or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _resolve_exec_profile_overrides(security_profile: Optional[str]) -> Dict[str, str]:
    """根据 CLI 安全预设返回 OpenClaw 运行时环境变量覆盖项。"""
    profile = str(security_profile or "").strip().lower()
    if not profile:
        return {}

    common = {
        "OPENCLAW_EXEC_HOST": "gateway",
        "OPENCLAW_EXEC_AUTO_ALLOW_SKILLS": "false",
        "OPENCLAW_ELEVATED_ENABLED": "false",
    }
    if profile == "relaxed":
        return {
            **common,
            "OPENCLAW_EXEC_STRICT_MODE": "false",
            "OPENCLAW_EXEC_UNSAFE_MODE": "true",
            "OPENCLAW_EXEC_SECURITY": "full",
            "OPENCLAW_EXEC_ASK": "off",
            "OPENCLAW_EXEC_ASK_FALLBACK": "full",
            "OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED": "false",
            "OPENCLAW_FS_WORKSPACE_ONLY": "false",
        }
    if profile == "strict":
        return {
            **common,
            "OPENCLAW_EXEC_STRICT_MODE": "true",
            "OPENCLAW_EXEC_UNSAFE_MODE": "false",
            "OPENCLAW_EXEC_SECURITY": "allowlist",
            "OPENCLAW_EXEC_ASK": "off",
            "OPENCLAW_EXEC_ASK_FALLBACK": "allowlist",
            "OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED": "true",
            "OPENCLAW_FS_WORKSPACE_ONLY": "false",
        }
    if profile == "strictest":
        return {
            **common,
            "OPENCLAW_EXEC_STRICT_MODE": "true",
            "OPENCLAW_EXEC_UNSAFE_MODE": "false",
            "OPENCLAW_EXEC_SECURITY": "deny",
            "OPENCLAW_EXEC_ASK": "off",
            "OPENCLAW_EXEC_ASK_FALLBACK": "deny",
            "OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED": "false",
            "OPENCLAW_FS_WORKSPACE_ONLY": "true",
        }
    raise ValueError(f"unsupported OpenClaw security profile: {security_profile}")


def _build_openclaw_env_vars(
    *,
    model_base_url: Optional[str] = None,
    model_api_key: Optional[str] = None,
    default_model: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    gateway_port: Optional[str] = None,
    public_port: Optional[str] = None,
    security_profile: Optional[str] = None,
) -> dict:
    """构建 OpenClaw 所需的环境变量，自动复用 OPENAI_* 环境变量"""
    env = {}
    default_provider_id = "ksyun"
    default_model_api = "openai-completions"
    default_model_base_url = "http://kspmas-internal.sdns.ksyun.com/v1"
    exec_profile_overrides = _resolve_exec_profile_overrides(security_profile)

    # 模型配置：客户端只透传用户显式配置和可选的 API Key；
    # 其余默认值交给镜像 bootstrap 兜底，避免创建请求把服务端默认行为短路掉。
    openclaw_explicit_model = default_model or _resolve_env("OPENCLAW_DEFAULT_MODEL")
    generic_model_preference = _resolve_env("OPENAI_MODEL_NAME", "MODEL_NAME", "LLM_MODEL")
    model_preference = openclaw_explicit_model or generic_model_preference
    explicit_base_url = (
        model_base_url
        or _resolve_env("OPENCLAW_MODEL_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
    )
    base_url = _resolve_model_base_url(explicit_base_url)
    api_key = (
        model_api_key
        or _resolve_env("OPENCLAW_MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "MODEL_API_KEY")
    )
    model = model_preference or "glm-5"
    explicit_provider_id = model_provider_id or _resolve_env("OPENCLAW_MODEL_PROVIDER_ID")
    inferred_provider_id = explicit_provider_id
    if not inferred_provider_id and model and "/" in model:
        inferred_provider_id = model.split("/", 1)[0].strip()
    provider_id = inferred_provider_id or default_provider_id
    resolved_gateway_port = (
        gateway_port
        or _resolve_env("OPENCLAW_GATEWAY_PORT", "PORT")
        or "8080"
    )
    resolved_public_port = (
        public_port
        or _resolve_env("OPENCLAW_PUBLIC_PORT")
        or "80"
    )
    explicit_model_api = _resolve_env("OPENCLAW_MODEL_API")
    model_api = explicit_model_api or default_model_api
    auth_mode = "trusted-proxy"
    trusted_proxy_user_header = (
        _resolve_env(
            "OPENCLAW_TRUSTED_PROXY_USER_HEADER",
            "OPENCLAW_GATEWAY_TRUSTED_PROXY_USER_HEADER",
        )
        or DEFAULT_TRUSTED_PROXY_USER_HEADER
    ).strip().lower()
    internal_trusted_proxy_user = (
        _resolve_env("OPENCLAW_INTERNAL_TRUSTED_PROXY_USER")
        or "openclaw-backend"
    )
    internal_trusted_proxy_user_header = (
        _resolve_env("OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER")
        or trusted_proxy_user_header
        or DEFAULT_TRUSTED_PROXY_USER_HEADER
    ).strip().lower()
    trusted_proxies = _normalize_csv_list(
        _resolve_env("OPENCLAW_TRUSTED_PROXIES") or "",
        default_items=DEFAULT_TRUSTED_PROXY_CIDRS,
    )
    browser_enabled = _resolve_env("OPENCLAW_BROWSER_ENABLED")
    browser_no_sandbox = _resolve_env("OPENCLAW_BROWSER_NO_SANDBOX") or "true"
    browser_headless = _resolve_env("OPENCLAW_BROWSER_HEADLESS") or "true"
    browser_executable = _resolve_env("OPENCLAW_BROWSER_EXECUTABLE_PATH", "OPENCLAW_BROWSER_EXECUTABLE")
    ui_locale = _normalize_ui_locale(_resolve_env("OPENCLAW_UI_LOCALE", "LANG", "LC_ALL"))
    exec_strict_mode_raw = (
        exec_profile_overrides.get("OPENCLAW_EXEC_STRICT_MODE")
        or _resolve_env("OPENCLAW_EXEC_STRICT_MODE", "OPENCLAW_EXEC_SAFE_MODE")
        or "false"
    )
    exec_strict_mode = _is_truthy(exec_strict_mode_raw)

    exec_host = exec_profile_overrides.get("OPENCLAW_EXEC_HOST") or _resolve_env("OPENCLAW_EXEC_HOST") or "gateway"
    exec_security = (
        exec_profile_overrides.get("OPENCLAW_EXEC_SECURITY")
        or _resolve_env("OPENCLAW_EXEC_SECURITY")
        or ("allowlist" if exec_strict_mode else "full")
    )
    exec_ask = exec_profile_overrides.get("OPENCLAW_EXEC_ASK") or _resolve_env("OPENCLAW_EXEC_ASK") or "off"
    exec_ask_fallback = (
        exec_profile_overrides.get("OPENCLAW_EXEC_ASK_FALLBACK")
        or _resolve_env("OPENCLAW_EXEC_ASK_FALLBACK")
        or ("allowlist" if exec_strict_mode else "full")
    )
    exec_auto_allow_skills = (
        exec_profile_overrides.get("OPENCLAW_EXEC_AUTO_ALLOW_SKILLS")
        or _resolve_env("OPENCLAW_EXEC_AUTO_ALLOW_SKILLS")
        or "false"
    )
    elevated_enabled = (
        exec_profile_overrides.get("OPENCLAW_ELEVATED_ENABLED")
        or _resolve_env("OPENCLAW_ELEVATED_ENABLED")
        or "false"
    )
    exec_default_allowlist_enabled = (
        exec_profile_overrides.get("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED")
        or _resolve_env("OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED")
        or (
        "true" if exec_strict_mode else "false"
    )
    )
    exec_allowlist = _resolve_env("OPENCLAW_EXEC_ALLOWLIST")
    fs_workspace_only = (
        exec_profile_overrides.get("OPENCLAW_FS_WORKSPACE_ONLY")
        or _resolve_env("OPENCLAW_FS_WORKSPACE_ONLY")
        or "false"
    )
    model_api_key_secret_source = _resolve_env("OPENCLAW_MODEL_API_KEY_SECRET_SOURCE") or "file"
    model_api_key_secret_file_path = _resolve_env("OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH")

    env["OPENCLAW_GATEWAY_BIND"] = "lan"
    env["OPENCLAW_GATEWAY_AUTH_MODE"] = auth_mode
    env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] = trusted_proxy_user_header or DEFAULT_TRUSTED_PROXY_USER_HEADER
    env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER"] = internal_trusted_proxy_user
    env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER"] = (
        internal_trusted_proxy_user_header or trusted_proxy_user_header or DEFAULT_TRUSTED_PROXY_USER_HEADER
    )
    env["OPENCLAW_TRUSTED_PROXIES"] = trusted_proxies
    env["OPENCLAW_GATEWAY_PORT"] = str(resolved_gateway_port)
    env["OPENCLAW_PUBLIC_PORT"] = str(resolved_public_port)
    if browser_enabled:
        env["OPENCLAW_BROWSER_ENABLED"] = browser_enabled
    env["OPENCLAW_BROWSER_NO_SANDBOX"] = browser_no_sandbox
    env["OPENCLAW_BROWSER_HEADLESS"] = browser_headless
    if browser_executable:
        env["OPENCLAW_BROWSER_EXECUTABLE_PATH"] = browser_executable
    env["OPENCLAW_UI_LOCALE"] = ui_locale
    env["OPENCLAW_EXEC_HOST"] = exec_host
    env["OPENCLAW_EXEC_STRICT_MODE"] = "true" if exec_strict_mode else "false"
    env["OPENCLAW_EXEC_UNSAFE_MODE"] = "false" if exec_strict_mode else "true"
    env["OPENCLAW_EXEC_SECURITY"] = exec_security
    env["OPENCLAW_EXEC_ASK"] = exec_ask
    env["OPENCLAW_EXEC_ASK_FALLBACK"] = exec_ask_fallback
    env["OPENCLAW_EXEC_AUTO_ALLOW_SKILLS"] = exec_auto_allow_skills
    env["OPENCLAW_ELEVATED_ENABLED"] = elevated_enabled
    env["OPENCLAW_EXEC_DEFAULT_ALLOWLIST_ENABLED"] = exec_default_allowlist_enabled
    env["OPENCLAW_FS_WORKSPACE_ONLY"] = fs_workspace_only
    env["OPENCLAW_MODEL_API_KEY_SECRET_SOURCE"] = model_api_key_secret_source
    if exec_allowlist:
        env["OPENCLAW_EXEC_ALLOWLIST"] = exec_allowlist
    if model_api_key_secret_file_path:
        env["OPENCLAW_MODEL_API_KEY_SECRET_FILE_PATH"] = model_api_key_secret_file_path

    if explicit_provider_id and provider_id != default_provider_id:
        env["OPENCLAW_MODEL_PROVIDER_ID"] = provider_id
    elif not explicit_provider_id and provider_id and provider_id != default_provider_id:
        env["OPENCLAW_MODEL_PROVIDER_ID"] = provider_id
    if explicit_model_api and model_api != default_model_api:
        env["OPENCLAW_MODEL_API"] = model_api
    if explicit_base_url and base_url and base_url != default_model_base_url:
        env["OPENCLAW_MODEL_BASE_URL"] = base_url
    if api_key:
        env["OPENCLAW_MODEL_API_KEY"] = api_key
    normalized_model = model.strip() if model else None
    catalog_model_id = None
    resolved_model = None
    if normalized_model:
        if "/" in normalized_model:
            _, catalog_model_id = normalized_model.split("/", 1)
            resolved_model = normalized_model
        else:
            catalog_model_id = normalized_model
            resolved_model = f"{provider_id}/{normalized_model}" if provider_id else normalized_model
        if openclaw_explicit_model:
            env["OPENCLAW_DEFAULT_MODEL"] = resolved_model
        elif generic_model_preference:
            env["OPENAI_MODEL_NAME"] = resolved_model

    # 额外的可选配置
    catalog = _resolve_env("OPENCLAW_MODEL_CATALOG_JSON")
    if catalog:
        env["OPENCLAW_MODEL_CATALOG_JSON"] = catalog
    origins = _resolve_env("OPENCLAW_ALLOWED_ORIGINS")
    if origins:
        env["OPENCLAW_ALLOWED_ORIGINS"] = _normalize_allowed_origins(origins)
    else:
        # 统一输出 JSON 数组字符串，兼容旧版 bootstrap（仅支持 JSON.parse）。
        env["OPENCLAW_ALLOWED_ORIGINS"] = json.dumps(["*"])
    allow_insecure_auth = _resolve_env("OPENCLAW_ALLOW_INSECURE_AUTH")
    env["OPENCLAW_ALLOW_INSECURE_AUTH"] = allow_insecure_auth if allow_insecure_auth else "true"
    disable_device_auth = _resolve_env("OPENCLAW_DISABLE_DEVICE_AUTH")
    env["OPENCLAW_DISABLE_DEVICE_AUTH"] = disable_device_auth if disable_device_auth else "true"
    for passthrough_key in [
        "OPENCLAW_AGENTSPACE_WPS_SID",
        "OPENCLAW_AGENTSPACE_APP_ID",
        "OPENCLAW_AGENTSPACE_CURRENT_USER",
        "OPENCLAW_WEB_FETCH_ENABLED",
        "OPENCLAW_WEB_SEARCH_PROVIDER",
        "OPENCLAW_WEB_SEARCH_BASE_URL",
        "OPENCLAW_WEB_SEARCH_MODEL",
        "OPENCLAW_WEB_SEARCH_API_KEY",
        "OPENCLAW_WEB_SEARCH_API_KEY_SECRET_SOURCE",
        "OPENCLAW_WEB_SEARCH_API_KEY_SECRET_PROVIDER",
        "OPENCLAW_WEB_SEARCH_API_KEY_SECRET_ID",
    ]:
        passthrough_value = _resolve_env(passthrough_key)
        if passthrough_value:
            env[passthrough_key] = passthrough_value

    return env


def _normalize_allowed_origins(raw: str) -> str:
    """标准化 OPENCLAW_ALLOWED_ORIGINS，统一输出 JSON 数组字符串。"""
    text = (raw or "").strip()
    if not text:
        return ""

    origins = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            origins = [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        # Backward compatible: 支持逗号/分号/空白分隔字符串。
        parts = [p.strip() for p in text.replace(";", ",").replace(" ", ",").split(",")]
        origins = [p.strip() for p in parts if p.strip()]

    if not origins:
        origins = [text]

    deduped = list(dict.fromkeys(origins))
    return json.dumps(deduped, ensure_ascii=False)


def _normalize_csv_list(raw: str, *, default_items: Optional[list[str]] = None) -> str:
    """标准化字符串列表为逗号分隔格式。"""
    text = (raw or "").strip()
    items: list[str] = []
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            parts = [p.strip() for p in text.replace(";", ",").replace(" ", ",").split(",")]
            items = [p for p in parts if p]

    if not items:
        items = [str(x).strip() for x in (default_items or []) if str(x).strip()]

    return ",".join(list(dict.fromkeys(items)))


def _parse_image(image: Optional[str]) -> tuple[str, str, str]:
    """解析镜像地址为 (namespace, repo, version)

    支持格式:
    - hub.kce.ksyun.com/ns/repo:tag → (ns, repo, tag)
    - ns/repo:tag → (ns, repo, tag)
    - 无输入 → 使用默认值
    """
    if not image:
        return DEFAULT_OPENCLAW_NAMESPACE, DEFAULT_OPENCLAW_REPO, DEFAULT_OPENCLAW_VERSION

    # 去掉 registry 域名前缀 (hub.kce.ksyun.com/)
    path = image
    if "/" in path:
        parts = path.split("/")
        if "." in parts[0]:
            # 有 registry 域名，去掉
            parts = parts[1:]
        if len(parts) >= 2:
            ns = parts[0]
            repo_version = "/".join(parts[1:])
        else:
            ns = "default"
            repo_version = parts[0]
    else:
        ns = "default"
        repo_version = path

    # 拆分 repo:version
    if ":" in repo_version:
        repo, version = repo_version.rsplit(":", 1)
    else:
        repo = repo_version
        version = "latest"

    return ns, repo, version


def _resolve_image_ref(image: Optional[str]) -> str:
    """解析并返回完整镜像地址。"""
    if image and str(image).strip():
        img = str(image).strip()
        first = img.split("/", 1)[0]
        if "." in first or ":" in first or first == "localhost":
            return img
        return f"{DEFAULT_OPENCLAW_REGISTRY}/{img}"
    return (
        f"{DEFAULT_OPENCLAW_REGISTRY}/"
        f"{DEFAULT_OPENCLAW_NAMESPACE}/{DEFAULT_OPENCLAW_REPO}:{DEFAULT_OPENCLAW_VERSION}"
    )


def _version_tuple(raw: str) -> tuple[int, ...]:
    """将版本字符串转为可比较的整数元组。"""
    parts = []
    for token in str(raw or "").strip().split("."):
        m = re.match(r"^(\d+)", token)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts)


def _is_version_newer(candidate: str, current: str) -> bool:
    """判断 candidate 是否高于 current。"""
    cand = _version_tuple(candidate)
    cur = _version_tuple(current)
    if not cand or not cur:
        return False
    n = max(len(cand), len(cur))
    cand = cand + (0,) * (n - len(cand))
    cur = cur + (0,) * (n - len(cur))
    return cand > cur


async def _fetch_bootstrap_config(region: str) -> Optional[Dict[str, Any]]:
    """从服务端获取客户端启动配置。失败时返回 None。"""
    from ksadk.api import AgentEngineClient
    from ksadk.version import VERSION as CLI_VERSION

    try:
        async with AgentEngineClient(region=region) as client:
            return await client.get_client_bootstrap_config(
                product="openclaw",
                framework="openclaw",
                region=region,
                client_type="cli",
                client_version=CLI_VERSION,
                locale=_resolve_env("OPENCLAW_UI_LOCALE", "LANG", "LC_ALL"),
            )
    except Exception as e:
        print_warn(f"拉取服务端默认配置失败，回退本地默认镜像: {e}")
        return None


def _extract_bootstrap_image(bootstrap_cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """从 bootstrap 配置中提取默认镜像。"""
    if not isinstance(bootstrap_cfg, dict):
        return None
    configs = bootstrap_cfg.get("configs")
    if not isinstance(configs, dict):
        return None
    value = configs.get("bootstrap.default_image") or configs.get("openclaw.default_image")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _print_bootstrap_hints(bootstrap_cfg: Optional[Dict[str, Any]]) -> None:
    """打印升级提示和公告（如果服务端下发）。"""
    if not isinstance(bootstrap_cfg, dict):
        return

    from ksadk.version import VERSION as CLI_VERSION

    configs = bootstrap_cfg.get("configs")
    if isinstance(configs, dict):
        latest = str(configs.get("upgrade.latest_cli_version") or "").strip()
        min_required = str(configs.get("upgrade.min_cli_version") or "").strip()
        upgrade_msg = str(configs.get("upgrade.message") or "").strip()

        if latest and _is_version_newer(latest, CLI_VERSION):
            hint = f"检测到 CLI 新版本: {latest} (当前 {CLI_VERSION})"
            if upgrade_msg:
                hint = f"{hint}，{upgrade_msg}"
            print_warn(hint)

        if min_required and _is_version_newer(min_required, CLI_VERSION):
            print_warn(
                f"当前 CLI 版本 {CLI_VERSION} 低于服务端建议最低版本 {min_required}，"
                "建议尽快升级后继续使用。"
            )

    notices = bootstrap_cfg.get("notices")
    if isinstance(notices, list):
        for item in notices:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            level = str(item.get("level") or "info").lower()
            if level in {"warn", "warning", "error"}:
                print_warn(f"平台公告: {message}")
            else:
                print_info(f"平台公告: {message}")
            break


def _flatten_agent_detail(agent: dict) -> dict:
    """将 GetAgent 响应转换为扁平结构，兼容旧字段和嵌套字段。"""
    basic = agent.get("basic", {}) if isinstance(agent, dict) else {}
    quick = agent.get("quick_access", {}) if isinstance(agent, dict) else {}
    deploy = agent.get("deployment", {}) if isinstance(agent, dict) else {}

    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id") or "",
        "name": basic.get("name") or agent.get("name") or "",
        "status": (basic.get("status") or agent.get("status") or "UNKNOWN").upper(),
        "framework": basic.get("framework") or agent.get("framework") or "",
        "region": basic.get("region") or agent.get("region") or "",
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint") or "",
        "artifact_path": deploy.get("artifact_path") or agent.get("artifact_path") or "",
        "created_at": basic.get("created_at") or agent.get("created_at") or "",
        "updated_at": basic.get("updated_at") or agent.get("updated_at") or "",
        "api_key": quick.get("api_key") or agent.get("api_key"),
    }


def _format_cli_timestamp(value: Optional[str], *, never_text: str = "-") -> str:
    raw = str(value or "").strip()
    if not raw:
        return never_text
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing = dt.astimezone(timezone(timedelta(hours=8)))
        beijing_text = beijing.strftime("%Y-%m-%d %H:%M:%S CST")
        utc_text = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"{beijing_text} ({utc_text})"
    except Exception:
        return raw


def _resolve_region(
    cli_region: Optional[str],
    state: Optional[dict],
) -> str:
    """解析 region: 显式参数 > state > 环境变量 > 默认值。"""
    return (
        cli_region
        or (state or {}).get("region")
        or _resolve_env("KSYUN_REGION")
        or "cn-beijing-6"
    )


async def _resolve_openclaw_detail_or_raise(
    agent_ref: Optional[str],
    *,
    region: Optional[str],
) -> tuple[str, dict[str, Any]]:
    from ksadk.api import AgentEngineClient
    from ksadk.deployment.state import load_state

    state = load_state(Path(".").resolve())
    resolved_region = _resolve_region(region, state)
    resolved = resolve_openclaw_ref(agent_ref, cwd=Path(".").resolve(), include_state=True)
    if not resolved:
        raise resolution_error(
            OPENCLAW_RESOURCE.missing_ref_message or "请指定 OpenClaw。",
            hints=list(OPENCLAW_RESOURCE.resolution_commands),
        )

    async with AgentEngineClient(region=resolved_region) as client:
        if resolved.value.startswith("ar-"):
            agent = await client.get_agent(agent_id=resolved.value)
        else:
            agent = await client.get_agent(name=resolved.value)

    if not agent:
        raise resolution_error(f"未找到 OpenClaw: {resolved.value}", hints=["agentengine openclaw list"])
    return resolved_region, _flatten_agent_detail(agent)


def _build_gateway_client(region: str, detail: dict[str, Any]) -> OpenClawGatewayClient:
    return OpenClawGatewayClient(
        region=region,
        agent_id=str(detail.get("agent_id") or "").strip(),
        agent_name=str(detail.get("name") or "").strip() or None,
    )


def _gateway_status_value(detail: dict[str, Any]) -> str:
    return str(detail.get("status") or "UNKNOWN").upper()


def _build_gateway_instance_check(detail: dict[str, Any]) -> dict[str, Any]:
    status_val = _gateway_status_value(detail)
    ready = status_val in OPENCLAW_GATEWAY_READY_STATUSES
    blocked = status_val in OPENCLAW_GATEWAY_BLOCKED_STATUSES
    payload: dict[str, Any] = {
        "name": "instance",
        "ok": not blocked,
        "status": status_val,
        "ready": ready,
    }
    if not blocked and not ready:
        payload["note"] = "控制面状态尚未进入 RUNNING，继续按网关实际连通性探测"
    return payload


async def _ensure_openclaw_gateway_available(
    region: str,
    detail: dict[str, Any],
    *,
    timeout_seconds: float = 60.0,
) -> None:
    status_val = str(detail.get("status") or "UNKNOWN").upper()
    if status_val in OPENCLAW_GATEWAY_BLOCKED_STATUSES:
        raise remote_error(
            f"目标 OpenClaw 当前状态为 {status_val}，暂不支持 gateway/channel 操作",
            details={"status": status_val},
        )
    if status_val not in OPENCLAW_GATEWAY_READY_STATUSES:
        await _wait_for_gateway_ready(region, detail, timeout_seconds=timeout_seconds)


def _emit_data_payload(title: str, payload: dict[str, Any], *, subtitle: Optional[str] = None) -> None:
    if is_json_output():
        emit_json(payload)
        return
    print_title(title, subtitle)
    console.print(json_dumps(payload), markup=False)


async def _wait_for_gateway_ready(
    region: str,
    detail: dict[str, Any],
    *,
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        gateway = _build_gateway_client(region, detail)
        try:
            await gateway.connect()
            return
        except Exception as exc:
            last_error = exc
        finally:
            await gateway.close()
        await asyncio.sleep(interval_seconds)

    message = f"gateway 在 {int(timeout_seconds)} 秒内未恢复可用"
    if last_error:
        message = f"{message}: {last_error}"
    raise OpenClawGatewayError(message)


async def _wait_for_gateway_reload_after_config_apply(
    gateway: OpenClawGatewayClient,
    region: str,
    detail: dict[str, Any],
    *,
    disconnect_timeout_seconds: float = 5.0,
    ready_timeout_seconds: float = 90.0,
) -> None:
    await gateway.wait_for_disconnect(timeout_ms=int(disconnect_timeout_seconds * 1000))
    await _wait_for_gateway_ready(region, detail, timeout_seconds=ready_timeout_seconds)


async def _fetch_channel_snapshot_with_retry(
    region: str,
    detail: dict[str, Any],
    *,
    probe: bool,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        gateway = _build_gateway_client(region, detail)
        try:
            await gateway.connect()
            return await gateway.channels_status(probe=probe, timeout_ms=8_000 if probe else None)
        except Exception as exc:
            last_error = exc
        finally:
            await gateway.close()
        await asyncio.sleep(interval_seconds)

    message = f"channel 状态在 {int(timeout_seconds)} 秒内未恢复可用"
    if last_error:
        message = f"{message}: {last_error}"
    raise OpenClawGatewayError(message)


def _channel_aliases(channel: str) -> tuple[str, ...]:
    spec = OPENCLAW_CHANNEL_SPECS.get(channel) or {}
    aliases = [channel]
    for candidate in (spec.get("channel_key"), spec.get("plugin_id")):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return tuple(aliases)


def _channel_config_section(config: dict[str, Any], channel: str) -> dict[str, Any]:
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    if not isinstance(channels, dict):
        return {}
    spec = OPENCLAW_CHANNEL_SPECS.get(channel) or {}
    section = channels.get(spec.get("channel_key") or channel)
    return section if isinstance(section, dict) else {}


def _channel_default_account_id(channel: str) -> str:
    spec = OPENCLAW_CHANNEL_SPECS.get(channel) or {}
    return str(spec.get("default_account_id") or "default")


def _is_channel_configured(
    channel: str,
    *,
    config: dict[str, Any],
    snapshot: Any,
) -> bool:
    if isinstance(snapshot, dict) and isinstance(snapshot.get("configured"), bool):
        return bool(snapshot.get("configured"))

    channel_cfg = _channel_config_section(config, channel)
    if channel == "weixin":
        accounts = channel_cfg.get("accounts")
        return isinstance(accounts, dict) and any(str(key).strip() for key in accounts.keys())
    if channel == "feishu":
        return bool(str(channel_cfg.get("appId") or "").strip() and str(channel_cfg.get("appSecret") or "").strip())
    if channel == "agentspace":
        accounts = channel_cfg.get("accounts")
        if not isinstance(accounts, dict):
            return False
        default_account = accounts.get(AGENTSPACE_DEFAULT_ACCOUNT_ID)
        if not isinstance(default_account, dict):
            return False
        return bool(
            str(default_account.get("token") or "").strip()
            or str(default_account.get("app_id") or "").strip()
        )
    return bool(channel_cfg)


def _build_channel_doctor_availability_check(
    *,
    name: str,
    available: bool,
    configured: bool,
    connect_required_message: str,
    connect_required_ok: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    if available:
        state = "ready"
        ok = True
        message = None
    elif configured:
        state = "missing"
        ok = False
        message = None
    else:
        state = "connect_required"
        ok = connect_required_ok
        message = connect_required_message

    payload = {
        "name": name,
        "ok": ok,
        "configured": configured,
        "state": state,
        **extra,
    }
    if message:
        payload["message"] = message
    return payload


def _extract_channel_snapshot(snapshot: Any, channel: Optional[str]) -> Any:
    if channel is None:
        return snapshot
    aliases = set(_channel_aliases(channel))
    if isinstance(snapshot, dict):
        for key in aliases:
            if key in snapshot:
                return snapshot[key]
        channels = snapshot.get("channels")
        if isinstance(channels, dict):
            for key in aliases:
                if key in channels:
                    return channels[key]
        if isinstance(channels, list):
            for item in channels:
                if not isinstance(item, dict):
                    continue
                candidates = {
                    str(item.get("id") or "").strip(),
                    str(item.get("channel") or "").strip(),
                    str(item.get("name") or "").strip(),
                    str(item.get("pluginId") or "").strip(),
                }
                if candidates & aliases:
                    return item
    return None


def _parse_time_arg(raw: Optional[str]) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise click.BadParameter("时间格式必须是 Unix 毫秒时间戳或 ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _render_terminal_qr(qr_url: str) -> None:
    if not is_stdout_tty():
        return
    try:
        import qrcode
    except Exception:
        print_warn("未检测到 Python `qrcode` 依赖，已退化为仅输出二维码 URL")
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    out = io.StringIO()
    qr.print_ascii(out=out, tty=False)
    console.print(out.getvalue(), markup=False)


def _ensure_local_node_tools() -> dict[str, str]:
    node_path = shutil.which("node")
    npx_path = shutil.which("npx")
    if not node_path or not npx_path:
        raise resolution_error(
            "本地缺少 `node` 或 `npx`，飞书接入依赖官方 onboarding 工具",
            hints=["先安装 Node.js，然后重试 `agentengine openclaw channel connect --channel feishu`"],
        )
    return {"node": node_path, "npx": npx_path}


async def _resolve_npx_cached_package_file(package_spec: str, relative_path: str) -> Path:
    tools = _ensure_local_node_tools()
    package_name = package_spec.strip()
    if package_name.startswith("@"):
        slash_idx = package_name.find("/")
        version_idx = package_name.rfind("@")
        if slash_idx > 0 and version_idx > slash_idx:
            package_name = package_name[:version_idx]
    elif "@" in package_name:
        package_name = package_name.split("@", 1)[0]
    warmup_cmd = [
        tools["npx"],
        "-y",
        "--package",
        package_spec,
        "-c",
        "true",
    ]
    completed = await asyncio.to_thread(
        subprocess.run,
        warmup_cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OpenClawGatewayError(
            f"无法准备官方 npm 包缓存: {completed.stderr.strip() or completed.stdout.strip() or package_spec}"
        )

    npm_cache_root = Path(
        os.getenv("NPM_CONFIG_CACHE")
        or os.getenv("npm_config_cache")
        or str(Path.home() / ".npm")
    )
    pattern = f"_npx/**/node_modules/{package_name}/{relative_path}"
    matches = list(npm_cache_root.glob(pattern))
    if not matches:
        raise OpenClawGatewayError(f"未找到官方 npm 包缓存文件: {package_name}/{relative_path}")
    return max(matches, key=lambda item: item.stat().st_mtime)


async def _run_feishu_onboarding(existing_app_id: Optional[str]) -> dict[str, Any]:
    tools = _ensure_local_node_tools()
    script_path = ""
    result_path = ""
    try:
        install_prompts_path = await _resolve_npx_cached_package_file(
            "@larksuite/openclaw-lark-tools@latest",
            "dist/utils/install-prompts.js",
        )
        script_body = textwrap.dedent(
            """
            const fs = require("fs");
            const { runInstallAuthFlow } = require(__INSTALL_PROMPTS_PATH__);

            (async () => {
              try {
                const result = await runInstallAuthFlow(
                  process.env.KSADK_FEISHU_APP_ID || undefined,
                  undefined,
                  {},
                  false,
                );
                fs.writeFileSync(process.env.KSADK_FEISHU_RESULT_PATH, JSON.stringify(result), "utf8");
              } catch (error) {
                console.error(error);
                process.exit(1);
              }
            })();
            """
        ).replace("__INSTALL_PROMPTS_PATH__", json.dumps(str(install_prompts_path)))
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as script_file:
            script_file.write(script_body.strip())
            script_path = script_file.name

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as result_file:
            result_path = result_file.name

        env = os.environ.copy()
        if existing_app_id:
            env["KSADK_FEISHU_APP_ID"] = existing_app_id
        env["KSADK_FEISHU_RESULT_PATH"] = result_path
        cmd = [tools["node"], script_path]
        completed = await asyncio.to_thread(subprocess.run, cmd, env=env, check=False)
        if completed.returncode != 0:
            raise OpenClawGatewayError("飞书官方 onboarding 流程执行失败")
        raw = Path(result_path).read_text(encoding="utf-8").strip()
        if not raw:
            raise OpenClawGatewayError("飞书官方 onboarding 未返回结果")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise OpenClawGatewayError("飞书官方 onboarding 返回了无效结果")
        return result
    finally:
        for temp_path in (script_path, result_path):
            if temp_path and Path(temp_path).exists():
                try:
                    Path(temp_path).unlink()
                except Exception:
                    pass


def _ensure_plugin_enabled(config: dict[str, Any], plugin_id: str) -> bool:
    changed = False
    plugins = config.setdefault("plugins", {})
    allow = plugins.setdefault("allow", [])
    if plugin_id not in allow:
        allow.append(plugin_id)
        changed = True
    entries = plugins.setdefault("entries", {})
    entry = entries.setdefault(plugin_id, {})
    if entry.get("enabled") is not True:
        entry["enabled"] = True
        changed = True
    return changed


def _extract_config_state(snapshot: dict[str, Any]) -> tuple[dict[str, Any], str]:
    config = snapshot.get("config")
    if not isinstance(config, dict):
        raise OpenClawGatewayError("config.get 未返回可编辑的配置快照")
    base_hash = str(snapshot.get("hash") or "").strip()
    if not base_hash and snapshot.get("exists", True):
        raise OpenClawGatewayError("config.get 未返回 base hash，请稍后重试")
    return copy.deepcopy(config), base_hash


def _resolve_weixin_account_id(
    config: dict[str, Any],
    *,
    account_id: Optional[str],
    create_if_missing: bool,
) -> str:
    if account_id:
        return str(account_id).strip()
    channels = config.get("channels") or {}
    weixin_cfg = channels.get(WEIXIN_PLUGIN_ID) or {}
    accounts = weixin_cfg.get("accounts")
    if isinstance(accounts, dict):
        account_keys = [str(key).strip() for key in accounts.keys() if str(key).strip()]
        if "default" in account_keys:
            return "default"
        if len(account_keys) == 1:
            return account_keys[0]
        if len(account_keys) > 1:
            raise click.ClickException("检测到多个微信账号，请显式传入 --account-id")
    if create_if_missing:
        return "default"
    raise click.ClickException("尚未检测到微信账号，请先执行 `agentengine openclaw channel connect --channel weixin`")


def _mutate_weixin_account_enabled(
    config: dict[str, Any],
    *,
    enabled: bool,
    account_id: Optional[str],
) -> tuple[bool, str]:
    changed = _ensure_plugin_enabled(config, WEIXIN_PLUGIN_ID) if enabled else False
    channels = config.setdefault("channels", {})
    weixin_cfg = channels.setdefault(WEIXIN_PLUGIN_ID, {})
    accounts = weixin_cfg.setdefault("accounts", {})
    resolved_account_id = _resolve_weixin_account_id(
        config,
        account_id=account_id,
        create_if_missing=enabled,
    )
    account_cfg = accounts.setdefault(resolved_account_id, {})
    if account_cfg.get("enabled") is not enabled:
        account_cfg["enabled"] = enabled
        changed = True
    return changed, resolved_account_id


def _mutate_feishu_enabled(
    config: dict[str, Any],
    *,
    enabled: bool,
    account_id: Optional[str],
) -> bool:
    normalized_account = str(account_id or "default").strip()
    if normalized_account not in {"", "default"}:
        raise click.ClickException("V1 仅支持飞书默认账号，`--account-id` 仅支持 default")
    changed = _ensure_plugin_enabled(config, FEISHU_PLUGIN_ID) if enabled else False
    channels = config.setdefault("channels", {})
    feishu_cfg = channels.setdefault(FEISHU_CHANNEL_KEY, {})
    if feishu_cfg.get("enabled") is not enabled:
        feishu_cfg["enabled"] = enabled
        changed = True
    return changed


def _mutate_feishu_connect_config(config: dict[str, Any], onboarding: dict[str, Any]) -> bool:
    changed = _ensure_plugin_enabled(config, FEISHU_PLUGIN_ID)
    channels = config.setdefault("channels", {})
    feishu_cfg = channels.setdefault(FEISHU_CHANNEL_KEY, {})

    desired_pairs = {
        "enabled": True,
        "appId": str(onboarding.get("appId") or "").strip(),
        "appSecret": str(onboarding.get("appSecret") or "").strip(),
        "domain": str(onboarding.get("domain") or "feishu").strip() or "feishu",
    }
    if not desired_pairs["appId"] or not desired_pairs["appSecret"]:
        raise OpenClawGatewayError("飞书 onboarding 结果缺少 appId/appSecret")

    defaults = {
        "connectionMode": "websocket",
        "requireMention": True,
    }
    for key, value in {**defaults, **desired_pairs}.items():
        if feishu_cfg.get(key) != value:
            feishu_cfg[key] = value
            changed = True

    user_info = onboarding.get("userInfo") if isinstance(onboarding.get("userInfo"), dict) else {}
    open_id = str(user_info.get("openId") or "").strip()
    if open_id:
        if feishu_cfg.get("dmPolicy") != "allowlist":
            feishu_cfg["dmPolicy"] = "allowlist"
            changed = True
        allow_from = feishu_cfg.setdefault("allowFrom", [])
        if open_id not in allow_from:
            allow_from.append(open_id)
            changed = True
        if feishu_cfg.get("groupPolicy") != "allowlist":
            feishu_cfg["groupPolicy"] = "allowlist"
            changed = True
        group_allow_from = feishu_cfg.setdefault("groupAllowFrom", [])
        if open_id not in group_allow_from:
            group_allow_from.append(open_id)
            changed = True
        groups = feishu_cfg.get("groups")
        if not isinstance(groups, dict) or "*" not in groups:
            feishu_cfg["groups"] = {"*": {"enabled": True}}
            changed = True
    else:
        if feishu_cfg.get("dmPolicy") not in {"pairing", "allowlist", "open"}:
            feishu_cfg["dmPolicy"] = "pairing"
            changed = True
        if not feishu_cfg.get("groupPolicy"):
            feishu_cfg["groupPolicy"] = "open"
            changed = True

    return changed


def _extract_agentspace_app_id(config: dict[str, Any]) -> Optional[str]:
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    agentspace_cfg = channels.get(AGENTSPACE_CHANNEL_KEY) if isinstance(channels, dict) else {}
    if not isinstance(agentspace_cfg, dict):
        return None
    accounts = agentspace_cfg.get("accounts")
    if isinstance(accounts, dict):
        default_account = accounts.get(AGENTSPACE_DEFAULT_ACCOUNT_ID)
        if isinstance(default_account, dict):
            app_id = str(default_account.get("app_id") or "").strip()
            if app_id:
                return app_id
    app_id = str(agentspace_cfg.get("app_id") or "").strip()
    return app_id or None


def _should_auto_open_browser() -> bool:
    if is_json_output():
        return False
    if str(os.getenv("SSH_TTY") or os.getenv("SSH_CONNECTION") or "").strip():
        return False
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(
        str(os.getenv("DISPLAY") or "").strip()
        or str(os.getenv("WAYLAND_DISPLAY") or "").strip()
        or str(os.getenv("BROWSER") or "").strip()
    )


def _encrypt_agentspace_token(wps_sid: str, *, app_id: Optional[str] = None) -> str:
    token = str(wps_sid or "").strip()
    if not token:
        raise OpenClawGatewayError("Agentspace 登录凭证为空，无法生成加密 token")

    key_source = str(app_id or "").strip() or AGENTSPACE_DEFAULT_KEY_SOURCE
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = hashlib.scrypt(key_source.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # pragma: no cover - validated via doctor and dependency constraints
        raise OpenClawGatewayError("缺少 cryptography 依赖，无法完成 Agentspace token 加密") from exc

    encrypted = AESGCM(key).encrypt(iv, token.encode("utf-8"), None)
    if len(encrypted) < 16:
        raise OpenClawGatewayError("Agentspace token 加密结果异常")
    cipher_bytes = encrypted[:-16]
    tag_bytes = encrypted[-16:]
    return f"{salt.hex()}:{iv.hex()}:{tag_bytes.hex()}:{cipher_bytes.hex()}"


async def _agentspace_http_request(
    *,
    method: str,
    url: str,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 20,
    allow_error_status: bool = False,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}

    def _send():
        return requests.request(
            method=method.upper(),
            url=url,
            json=json_body if json_body is not None else None,
            headers=request_headers,
            timeout=timeout,
        )

    response = await asyncio.to_thread(_send)
    status_code = int(response.status_code)
    try:
        payload = response.json()
    except Exception:
        payload = response.text.strip()

    if status_code >= 400 and not allow_error_status:
        message = str(payload) if isinstance(payload, (str, dict, list)) else "unknown"
        raise OpenClawGatewayError(f"Agentspace API 请求失败: HTTP {status_code} {message}")
    return {
        "ok": 200 <= status_code < 300,
        "status_code": status_code,
        "payload": payload,
    }


def _agentspace_extract_error_detail(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    if not isinstance(payload, dict):
        text = str(payload).strip()
        return text or None

    code = payload.get("code")
    msg = str(payload.get("msg") or payload.get("message") or "").strip()
    detail_parts: list[str] = []
    data = payload.get("data")
    if isinstance(data, dict):
        for key, value in data.items():
            value_text = str(value or "").strip()
            if value_text:
                detail_parts.append(f"{key}={value_text}")
    elif data is not None:
        value_text = str(data).strip()
        if value_text:
            detail_parts.append(value_text)

    summary_parts: list[str] = []
    if code not in (None, "", 0, "0"):
        summary_parts.append(f"code={code}")
    if msg:
        summary_parts.append(f"msg={msg}")
    if detail_parts:
        summary_parts.append(f"data={'; '.join(detail_parts[:3])}")
    if summary_parts:
        return ", ".join(summary_parts)
    return None


def _agentspace_is_fatal_oauth_error(payload: Any, *, status_code: int) -> bool:
    if status_code >= 400:
        return True
    if not isinstance(payload, dict):
        return False

    code = payload.get("code")
    if code in (None, "", 0, "0"):
        return False

    combined = _agentspace_extract_error_detail(payload) or ""
    normalized = combined.lower()
    fatal_markers = (
        "invalid parameter",
        "permission",
        "no permission",
        "forbidden",
        "denied",
        "unauthorized",
        "app_id",
    )
    return any(marker in normalized for marker in fatal_markers)


def _agentspace_is_pending_user_token_response(payload: Any, *, status_code: int) -> bool:
    if status_code < 500:
        return False
    detail = (_agentspace_extract_error_detail(payload) or "").lower()
    return "nonetype" in detail and "strip" in detail


def _build_agentspace_oauth_error(
    stage: str,
    *,
    payload: Any,
    status_code: int,
    app_id: Optional[str],
) -> OpenClawGatewayError:
    detail = _agentspace_extract_error_detail(payload) or f"HTTP {status_code}"
    message = f"Agentspace {stage}失败: {detail}"
    if app_id:
        message += (
            f"。当前使用 app_id={app_id}；如果这是历史残留，可直接重试当前命令"
            "（默认不复用旧 app_id），或显式传入有权限的 --app-id"
        )
    return OpenClawGatewayError(message)


def _agentspace_response_error_detail(payload: Any, *, status_code: int) -> Optional[str]:
    detail = _agentspace_extract_error_detail(payload)
    if status_code >= 400:
        return detail or f"HTTP {status_code}"
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code in (None, "", 0, "0"):
        return None
    return detail or f"code={code}"


async def _agentspace_fetch_current_user_by_sid(wps_sid: str) -> dict[str, str]:
    sid = str(wps_sid or "").strip()
    if not sid:
        raise OpenClawGatewayError("Agentspace 登录凭证为空，无法直接配置 channel")

    current_resp = await _agentspace_http_request(
        method="GET",
        url=AGENTSPACE_CURRENT_USER_API,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Cookie": f"wps_sid={sid}",
            "Referer": "https://agentspace.wps.cn/agents",
        },
        timeout=20,
        allow_error_status=True,
    )
    current_payload = current_resp.get("payload")
    error_detail = _agentspace_response_error_detail(
        current_payload,
        status_code=int(current_resp.get("status_code") or 0),
    )
    if error_detail:
        raise OpenClawGatewayError(f"提供的 wps_sid 无法通过 Agentspace 鉴权: {error_detail}")

    current_data = current_payload.get("data") if isinstance(current_payload, dict) else {}
    nickname = (
        str(current_data.get("nickname") or "").strip()
        if isinstance(current_data, dict)
        else ""
    )
    return {
        "wps_sid": sid,
        "current_user": nickname,
        "app_id": "",
        "login_url": "",
    }


async def _agentspace_cloud_server_oauth(
    app_id: Optional[str],
    *,
    open_browser: bool,
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 5,
) -> dict[str, str]:
    state = uuid.uuid4().hex
    login_body: dict[str, Any] = {"state": state}
    if app_id:
        login_body["app_id"] = str(app_id).strip()

    login_resp = await _agentspace_http_request(
        method="POST",
        url=AGENTSPACE_LOGIN_URL_API,
        json_body=login_body,
        timeout=20,
    )
    login_payload = login_resp.get("payload")
    login_data = login_payload.get("data") if isinstance(login_payload, dict) else {}
    if not isinstance(login_data, dict):
        detail = _agentspace_extract_error_detail(login_payload)
        if detail:
            raise _build_agentspace_oauth_error(
                "login_url",
                payload=login_payload,
                status_code=int(login_resp.get("status_code") or 0),
                app_id=app_id,
            )
        raise OpenClawGatewayError("Agentspace login_url 返回格式异常")
    code = str(login_data.get("code") or "").strip()
    login_url = str(login_data.get("url") or "").strip()
    resolved_app_id = str(login_data.get("app_id") or app_id or "").strip()
    if not code or not login_url:
        detail = _agentspace_extract_error_detail(login_payload)
        if detail:
            raise _build_agentspace_oauth_error(
                "login_url",
                payload=login_payload,
                status_code=int(login_resp.get("status_code") or 0),
                app_id=resolved_app_id or app_id,
            )
        raise OpenClawGatewayError("Agentspace login_url 未返回有效 code/url")

    print_success("请在浏览器完成 Agentspace 登录授权")
    print_kv("登录链接", login_url, value_style="#58a6ff")
    if open_browser:
        webbrowser.open(login_url)

    deadline = time.monotonic() + timeout_seconds
    last_pending_detail: Optional[str] = None
    while time.monotonic() < deadline:
        token_body = {
            "code": code,
            "state": state,
        }
        token_resp = await _agentspace_http_request(
            method="POST",
            url=AGENTSPACE_USER_TOKEN_API,
            json_body=token_body,
            timeout=20,
            allow_error_status=True,
        )
        token_payload = token_resp.get("payload")
        token_status_code = int(token_resp.get("status_code") or 0)
        if _agentspace_is_pending_user_token_response(
            token_payload,
            status_code=token_status_code,
        ):
            last_pending_detail = _agentspace_extract_error_detail(token_payload)
            await asyncio.sleep(poll_interval_seconds)
            continue
        if _agentspace_is_fatal_oauth_error(
            token_payload,
            status_code=token_status_code,
        ):
            raise _build_agentspace_oauth_error(
                "user_token",
                payload=token_payload,
                status_code=token_status_code,
                app_id=resolved_app_id,
            )
        token_data = token_payload.get("data") if isinstance(token_payload, dict) else {}
        user_token = str(token_data.get("token") or "").strip() if isinstance(token_data, dict) else ""
        if user_token:
            current_resp = await _agentspace_http_request(
                method="GET",
                url=AGENTSPACE_CURRENT_USER_API,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Cookie": f"wps_sid={user_token}",
                    "Referer": "https://agentspace.wps.cn/agents",
                },
                timeout=20,
                allow_error_status=True,
            )
            current_payload = current_resp.get("payload")
            current_data = current_payload.get("data") if isinstance(current_payload, dict) else {}
            nickname = (
                str(current_data.get("nickname") or "").strip()
                if isinstance(current_data, dict)
                else ""
            )
            return {
                "wps_sid": user_token,
                "current_user": nickname,
                "app_id": resolved_app_id,
                "login_url": login_url,
            }
        await asyncio.sleep(poll_interval_seconds)

    if last_pending_detail:
        raise OpenClawGatewayError(
            "等待 Agentspace 登录确认超时；上游在授权完成前返回了内部错误，"
            "请确认浏览器回调是否成功。若页面提示 app_id 无权限，可改用 --wps-sid"
            " 或显式传入有权限的 --app-id"
        )
    raise OpenClawGatewayError("等待 Agentspace 登录确认超时（5 分钟）")


def _mutate_agentspace_connect_config(config: dict[str, Any], oauth_result: dict[str, str]) -> bool:
    changed = _ensure_plugin_enabled(config, AGENTSPACE_PLUGIN_ID)
    channels = config.setdefault("channels", {})
    agentspace_cfg = channels.setdefault(AGENTSPACE_CHANNEL_KEY, {})
    accounts = agentspace_cfg.setdefault("accounts", {})
    default_account = accounts.setdefault(AGENTSPACE_DEFAULT_ACCOUNT_ID, {})
    device_uuid = str(default_account.get("device_uuid") or "").strip() or str(uuid.uuid4())

    encrypted_token = _encrypt_agentspace_token(
        str(oauth_result.get("wps_sid") or "").strip(),
        app_id=str(oauth_result.get("app_id") or "").strip(),
    )
    desired_pairs = {
        "enabled": True,
        "token": encrypted_token,
        "currentUser": str(oauth_result.get("current_user") or "").strip(),
        "app_id": str(oauth_result.get("app_id") or "").strip(),
        "device_uuid": device_uuid,
    }
    for key, value in desired_pairs.items():
        if default_account.get(key) != value:
            default_account[key] = value
            changed = True

    if not str(agentspace_cfg.get("dmPolicy") or "").strip():
        agentspace_cfg["dmPolicy"] = "open"
        changed = True
    allow_from = agentspace_cfg.get("allowFrom")
    if not isinstance(allow_from, list) or len(allow_from) == 0:
        agentspace_cfg["allowFrom"] = ["*"]
        changed = True

    return changed


def _mutate_agentspace_enabled(
    config: dict[str, Any],
    *,
    enabled: bool,
    account_id: Optional[str],
) -> bool:
    normalized_account = str(account_id or AGENTSPACE_DEFAULT_ACCOUNT_ID).strip()
    if normalized_account not in {"", AGENTSPACE_DEFAULT_ACCOUNT_ID}:
        raise click.ClickException("V1 仅支持 agentspace 默认账号，`--account-id` 仅支持 default")
    changed = _ensure_plugin_enabled(config, AGENTSPACE_PLUGIN_ID) if enabled else False
    channels = config.setdefault("channels", {})
    agentspace_cfg = channels.setdefault(AGENTSPACE_CHANNEL_KEY, {})
    accounts = agentspace_cfg.setdefault("accounts", {})
    default_account = accounts.setdefault(AGENTSPACE_DEFAULT_ACCOUNT_ID, {})
    if default_account.get("enabled") is not enabled:
        default_account["enabled"] = enabled
        changed = True
    return changed


def _check_agentspace_local_deps() -> dict[str, Any]:
    crypto_path = None
    crypto_error = None
    try:
        import cryptography as _cryptography  # type: ignore
        crypto_path = str(getattr(_cryptography, "__file__", "") or "")
    except Exception as exc:
        crypto_error = str(exc)
    requests_path = str(getattr(requests, "__file__", "") or "")
    return {
        "ok": bool(crypto_path and requests_path),
        "cryptography": crypto_path or None,
        "requests": requests_path or None,
        "cryptography_error": crypto_error,
    }


@click.group("openclaw", context_settings=CONTEXT_SETTINGS, help=build_resource_group_help(OPENCLAW_RESOURCE))
def openclaw():
    pass


@openclaw.group(
    "gateway",
    context_settings=CONTEXT_SETTINGS,
    help="OpenClaw gateway 入口、日志与诊断。",
)
def openclaw_gateway():
    pass


@openclaw_gateway.command("open", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@cli_output_option()
def gateway_open(agent_ref: Optional[str], region: Optional[str], no_open: bool, output_mode: str | None):
    """打开 OpenClaw gateway Dashboard。"""
    _ = output_mode
    no_open = no_open or is_json_output()

    async def _run():
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        await _ensure_openclaw_gateway_available(resolved_region, detail)
        gateway = _build_gateway_client(resolved_region, detail)
        try:
            info = await gateway.build_access_info()
        finally:
            await gateway.close()

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "status": detail.get("status"),
            "dashboard_url": info.access_url,
            "ws_url": info.ws_url,
            "auth_mode": "cookie-session",
            "link_id": info.link_id,
            "expires_at": info.expires_at,
        }
        if is_json_output():
            emit_json(payload)
            return

        print_success("OpenClaw gateway 短链已生成")
        print_kv("OpenClaw", str(detail.get("name") or detail.get("agent_id") or "-"))
        print_kv("Dashboard", info.access_url, value_style="#58a6ff")
        print_kv("WebSocket", info.ws_url, value_style="#58a6ff")
        print_kv("认证方式", "cookie-session")
        if not no_open:
            webbrowser.open(info.access_url)

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context="打开 gateway 失败", argv=["openclaw", "gateway", "open"])


@openclaw_gateway.command("ws-url", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def gateway_ws_url(agent_ref: Optional[str], region: Optional[str], output_mode: str | None):
    """打印 gateway 短链与推导后的 websocket 地址。"""
    _ = output_mode

    async def _run():
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        await _ensure_openclaw_gateway_available(resolved_region, detail)
        gateway = _build_gateway_client(resolved_region, detail)
        try:
            info = await gateway.build_access_info()
        finally:
            await gateway.close()

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "dashboard_url": info.access_url,
            "ws_url": info.ws_url,
            "auth_mode": "cookie-session",
            "note": "该 ws-url 依赖短链 cookie session，不承诺长期复用。",
        }
        _emit_data_payload("OpenClaw Gateway 连接信息", payload, subtitle=str(detail.get("name") or "-"))

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context="获取 gateway ws-url 失败", argv=["openclaw", "gateway", "ws-url"])


@openclaw_gateway.command("logs", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--instance", default=None, help="实例名；不填则查询全部实例")
@click.option("--log-type", type=click.Choice(["stdout", "log"], case_sensitive=False), default="stdout", show_default=True, help="日志类型")
@click.option("--start-time", default=None, help="开始时间，支持 Unix 毫秒或 ISO-8601")
@click.option("--end-time", default=None, help="结束时间，支持 Unix 毫秒或 ISO-8601")
@cli_output_option()
def gateway_logs(
    agent_ref: Optional[str],
    region: Optional[str],
    instance: Optional[str],
    log_type: str,
    start_time: Optional[str],
    end_time: Optional[str],
    output_mode: str | None,
):
    """读取 OpenClaw gateway 日志。"""
    _ = output_mode

    async def _run():
        from ksadk.api import AgentEngineClient

        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        async with AgentEngineClient(region=resolved_region) as client:
            resp = await client.get_agent_logs(
                agent_id=str(detail.get("agent_id") or ""),
                instance=instance,
                log_type=log_type,
                start_time=_parse_time_arg(start_time),
                end_time=_parse_time_arg(end_time),
                page=1,
                page_size=200,
            )

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "instance": resp.get("instance"),
            "log_type": resp.get("log_type"),
            "total": resp.get("total"),
            "logs": resp.get("logs", []),
        }
        if is_json_output():
            emit_json(payload)
            return

        print_title("OpenClaw Gateway 日志", str(detail.get("name") or detail.get("agent_id") or "-"))
        print_kv("实例", str(resp.get("instance") or "all"))
        print_kv("日志类型", str(resp.get("log_type") or log_type))
        print_kv("日志条数", str(resp.get("total") or 0))
        logs = resp.get("logs", []) or []
        if not logs:
            print_warn("没有查询到日志")
            return
        for line in logs:
            console.print(str(line), markup=False)

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context="获取 gateway 日志失败", argv=["openclaw", "gateway", "logs"])


@openclaw_gateway.command("doctor", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def gateway_doctor(agent_ref: Optional[str], region: Optional[str], output_mode: str | None):
    """检查 gateway 短链、cookie 与 websocket 链路。"""
    _ = output_mode

    async def _run() -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        checks.append(_build_gateway_instance_check(detail))

        if checks[-1]["ok"]:
            gateway = _build_gateway_client(resolved_region, detail)
            try:
                info = await gateway.build_access_info()
                checks.append(
                    {
                        "name": "dashboard_short_link",
                        "ok": True,
                        "dashboard_url": info.access_url,
                        "ws_url": info.ws_url,
                    }
                )
                hello = await gateway.connect()
                checks.append(
                    {
                        "name": "cookie_ws_handshake",
                        "ok": True,
                        "methods": len(gateway.methods),
                    }
                )
                cfg = await gateway.config_get()
                checks.append(
                    {
                        "name": "gateway_rpc",
                        "ok": isinstance(cfg, dict),
                        "config_path": cfg.get("path"),
                    }
                )
            except Exception as exc:
                checks.append({"name": "gateway_connectivity", "ok": False, "error": str(exc)})
            finally:
                await gateway.close()

        return {
            "ok": all(bool(item.get("ok")) for item in checks),
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "checks": checks,
        }

    try:
        payload = asyncio.run(_run())
        _emit_data_payload("OpenClaw Gateway Doctor", payload, subtitle=str(payload.get("name") or "-"))
    except Exception as e:
        _abort_openclaw_error(e, context="gateway doctor 执行失败", argv=["openclaw", "gateway", "doctor"])


@openclaw.group(
    "channel",
    context_settings=CONTEXT_SETTINGS,
    help="OpenClaw Channel 统一入口。",
)
def openclaw_channel():
    pass


@openclaw_channel.command("status", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), default=None, help="指定 channel")
@click.option("--probe", is_flag=True, help="触发远端 probe 刷新 channel 快照")
@cli_output_option()
def channel_status(
    agent_ref: Optional[str],
    region: Optional[str],
    channel: Optional[str],
    probe: bool,
    output_mode: str | None,
):
    """查看远端 channel 状态快照。"""
    _ = output_mode
    normalized_channel = str(channel).lower() if channel else None

    async def _run():
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        await _ensure_openclaw_gateway_available(resolved_region, detail)
        gateway = _build_gateway_client(resolved_region, detail)
        try:
            await gateway.connect()
            snapshot = await gateway.channels_status(probe=probe, timeout_ms=8_000 if probe else None)
        finally:
            await gateway.close()

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "channel": normalized_channel,
            "probe": probe,
            "snapshot": snapshot,
            "selected": _extract_channel_snapshot(snapshot, normalized_channel),
        }
        _emit_data_payload("OpenClaw Channel 状态", payload, subtitle=str(detail.get("name") or "-"))

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context="获取 channel 状态失败", argv=["openclaw", "channel", "status"])


@openclaw_channel.command("connect", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", "channel_name", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), required=True, help="目标 channel")
@click.option("--open-qr", is_flag=True, help="在本地浏览器额外打开二维码/登录链接")
@click.option("--app-id", "agentspace_app_id", default=None, help="显式指定 Agentspace app_id（默认不复用历史值）")
@click.option("--reuse-app-id", is_flag=True, help="复用远端已保存的 Agentspace app_id")
@click.option("--wps-sid", "agentspace_wps_sid", default=None, help="跳过网页登录，直接使用现成的 wps_sid 配置 Agentspace")
def channel_connect(
    agent_ref: Optional[str],
    region: Optional[str],
    channel_name: str,
    open_qr: bool,
    agentspace_app_id: Optional[str],
    reuse_app_id: bool,
    agentspace_wps_sid: Optional[str],
):
    """连接微信、飞书或 Agentspace channel。"""
    normalized_channel = channel_name.lower()

    async def _run():
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        await _ensure_openclaw_gateway_available(resolved_region, detail)

        if normalized_channel == "weixin":
            preflight_changed = False
            preflight_gateway = _build_gateway_client(resolved_region, detail)
            try:
                await preflight_gateway.connect()
                cfg_snapshot = await preflight_gateway.config_get()
                config, base_hash = _extract_config_state(cfg_snapshot)
                preflight_changed, _ = _mutate_weixin_account_enabled(
                    config,
                    enabled=True,
                    account_id="default",
                )
                if preflight_changed:
                    await preflight_gateway.config_apply(
                        config=config,
                        base_hash=base_hash,
                        note="ksadk seed weixin channel config",
                    )
                    await _wait_for_gateway_reload_after_config_apply(
                        preflight_gateway,
                        resolved_region,
                        detail,
                    )
            finally:
                await preflight_gateway.close()

            gateway = _build_gateway_client(resolved_region, detail)
            try:
                await gateway.connect()
                start = await gateway.web_login_start(force=False, timeout_ms=30_000)
                qr_url = str(start.get("qrDataUrl") or "").strip()
                session_key = str(start.get("sessionKey") or "").strip()
                if not qr_url:
                    raise OpenClawGatewayError("微信扫码登录未返回二维码 URL")

                print_success("请使用微信扫码完成连接")
                print_kv("二维码链接", qr_url, value_style="#58a6ff")
                _render_terminal_qr(qr_url)
                if open_qr:
                    webbrowser.open(qr_url)

                wait_result = await gateway.web_login_wait(
                    account_id=session_key or None,
                    timeout_ms=120_000,
                )
            finally:
                await gateway.close()

            snapshot = await _fetch_channel_snapshot_with_retry(
                resolved_region,
                detail,
                probe=True,
            )

            payload = {
                "ok": True,
                "agent_id": detail.get("agent_id"),
                "name": detail.get("name"),
                "region": resolved_region,
                "channel": normalized_channel,
                "qr_url": qr_url,
                "login": wait_result,
                "status": _extract_channel_snapshot(snapshot, normalized_channel),
            }
            _emit_data_payload("OpenClaw Channel Connect", payload, subtitle="weixin")
            return

        if normalized_channel == "agentspace":
            bootstrap_gateway = _build_gateway_client(resolved_region, detail)
            try:
                await bootstrap_gateway.connect()
                cfg_snapshot = await bootstrap_gateway.config_get()
            finally:
                await bootstrap_gateway.close()
            config, _ = _extract_config_state(cfg_snapshot)
            existing_app_id = _extract_agentspace_app_id(config)
            selected_app_id = str(agentspace_app_id or "").strip() or None
            if not selected_app_id and reuse_app_id:
                selected_app_id = existing_app_id
                if selected_app_id:
                    print_info(f"复用远端已保存的 Agentspace app_id: {selected_app_id}")
            elif selected_app_id:
                print_info(f"使用显式 Agentspace app_id: {selected_app_id}")
            elif existing_app_id:
                print_info("检测到已保存的 Agentspace app_id，当前默认忽略；如需复用请传 --reuse-app-id")

            selected_wps_sid = str(agentspace_wps_sid or "").strip() or None
            if selected_wps_sid:
                print_info("检测到显式提供的 wps_sid，跳过 Agentspace 网页授权流程")
                oauth_result = await _agentspace_fetch_current_user_by_sid(selected_wps_sid)
                if selected_app_id:
                    oauth_result["app_id"] = selected_app_id
            else:
                oauth_result = await _agentspace_cloud_server_oauth(
                    selected_app_id,
                    open_browser=open_qr or _should_auto_open_browser(),
                )

            apply_gateway = _build_gateway_client(resolved_region, detail)
            changed = False
            try:
                await apply_gateway.connect()
                fresh_cfg_snapshot = await apply_gateway.config_get()
                fresh_config, fresh_base_hash = _extract_config_state(fresh_cfg_snapshot)
                changed = _mutate_agentspace_connect_config(fresh_config, oauth_result)
                if changed:
                    await apply_gateway.config_apply(
                        config=fresh_config,
                        base_hash=fresh_base_hash,
                        note="ksadk configure agentspace channel",
                    )
                    await _wait_for_gateway_reload_after_config_apply(
                        apply_gateway,
                        resolved_region,
                        detail,
                    )
            finally:
                await apply_gateway.close()

            snapshot = await _fetch_channel_snapshot_with_retry(
                resolved_region,
                detail,
                probe=True,
            )
            payload = {
                "ok": True,
                "agent_id": detail.get("agent_id"),
                "name": detail.get("name"),
                "region": resolved_region,
                "channel": normalized_channel,
                "configured": True,
                "changed": changed,
                "app_id": oauth_result.get("app_id"),
                "current_user": oauth_result.get("current_user"),
                "login_url": oauth_result.get("login_url"),
                "status": _extract_channel_snapshot(snapshot, normalized_channel),
            }
            _emit_data_payload("OpenClaw Channel Connect", payload, subtitle="agentspace")
            return

        bootstrap_gateway = _build_gateway_client(resolved_region, detail)
        try:
            await bootstrap_gateway.connect()
            cfg_snapshot = await bootstrap_gateway.config_get()
        finally:
            await bootstrap_gateway.close()

        config, _ = _extract_config_state(cfg_snapshot)
        existing_app_id = str(
            ((config.get("channels") or {}).get(FEISHU_CHANNEL_KEY) or {}).get("appId") or ""
        ).strip() or None
        onboarding = await _run_feishu_onboarding(existing_app_id)
        changed_config = copy.deepcopy(config)
        changed = _mutate_feishu_connect_config(changed_config, onboarding)
        base_hash = str(cfg_snapshot.get("hash") or "").strip()
        if changed:
            apply_gateway = _build_gateway_client(resolved_region, detail)
            try:
                await apply_gateway.connect()
                await apply_gateway.config_apply(
                    config=changed_config,
                    base_hash=base_hash,
                    note="ksadk configure feishu channel",
                )
                await _wait_for_gateway_reload_after_config_apply(
                    apply_gateway,
                    resolved_region,
                    detail,
                )
            finally:
                await apply_gateway.close()

        snapshot = await _fetch_channel_snapshot_with_retry(
            resolved_region,
            detail,
            probe=True,
        )

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "channel": normalized_channel,
            "configured": True,
            "changed": changed,
            "status": _extract_channel_snapshot(snapshot, normalized_channel),
        }
        _emit_data_payload("OpenClaw Channel Connect", payload, subtitle="feishu")

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context="channel connect 失败", argv=["openclaw", "channel", "connect"])


def _run_channel_toggle_command(
    *,
    action: str,
    enabled: bool,
    agent_ref: Optional[str],
    region: Optional[str],
    channel_name: str,
    account_id: Optional[str],
) -> None:
    normalized_channel = channel_name.lower()

    async def _run():
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        await _ensure_openclaw_gateway_available(resolved_region, detail)

        gateway = _build_gateway_client(resolved_region, detail)
        try:
            await gateway.connect()
            cfg_snapshot = await gateway.config_get()
            config, base_hash = _extract_config_state(cfg_snapshot)
            if normalized_channel == "weixin":
                changed, resolved_account_id = _mutate_weixin_account_enabled(
                    config,
                    enabled=enabled,
                    account_id=account_id,
                )
            elif normalized_channel == "agentspace":
                changed = _mutate_agentspace_enabled(
                    config,
                    enabled=enabled,
                    account_id=account_id,
                )
                resolved_account_id = _channel_default_account_id(normalized_channel)
            else:
                changed = _mutate_feishu_enabled(config, enabled=enabled, account_id=account_id)
                resolved_account_id = _channel_default_account_id(normalized_channel)

            if changed:
                await gateway.config_apply(
                    config=config,
                    base_hash=base_hash,
                    note=f"ksadk {action} {normalized_channel} channel",
                )
                await _wait_for_gateway_reload_after_config_apply(
                    gateway,
                    resolved_region,
                    detail,
                )
        finally:
            await gateway.close()

        snapshot = await _fetch_channel_snapshot_with_retry(
            resolved_region,
            detail,
            probe=True,
        )

        payload = {
            "ok": True,
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "channel": normalized_channel,
            "account_id": resolved_account_id,
            "changed": changed,
            "enabled": enabled,
            "status": _extract_channel_snapshot(snapshot, normalized_channel),
        }
        _emit_data_payload(f"OpenClaw Channel {action.title()}", payload, subtitle=normalized_channel)

    try:
        asyncio.run(_run())
    except Exception as e:
        _abort_openclaw_error(e, context=f"channel {action} 失败", argv=["openclaw", "channel", action])


@openclaw_channel.command("enable", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", "channel_name", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), required=True, help="目标 channel")
@click.option("--account-id", default=None, help="账号 ID；飞书 V1 仅支持 default")
def channel_enable(
    agent_ref: Optional[str],
    region: Optional[str],
    channel_name: str,
    account_id: Optional[str],
):
    """启用远端 channel 配置。"""
    _run_channel_toggle_command(
        action="enable",
        enabled=True,
        agent_ref=agent_ref,
        region=region,
        channel_name=channel_name,
        account_id=account_id,
    )


@openclaw_channel.command("disable", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", "channel_name", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), required=True, help="目标 channel")
@click.option("--account-id", default=None, help="账号 ID；飞书 V1 仅支持 default")
def channel_disable(
    agent_ref: Optional[str],
    region: Optional[str],
    channel_name: str,
    account_id: Optional[str],
):
    """禁用远端 channel 配置。"""
    _run_channel_toggle_command(
        action="disable",
        enabled=False,
        agent_ref=agent_ref,
        region=region,
        channel_name=channel_name,
        account_id=account_id,
    )


@openclaw_channel.command("doctor", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), default=None, help="指定 channel")
@cli_output_option()
def channel_doctor(agent_ref: Optional[str], region: Optional[str], channel: Optional[str], output_mode: str | None):
    """检查 channel 接入前置条件。"""
    _ = output_mode
    normalized_channel = str(channel).lower() if channel else None

    async def _run():
        checks: list[dict[str, Any]] = []
        resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
        checks.append(_build_gateway_instance_check(detail))

        snapshot = None
        methods: list[str] = []
        config: dict[str, Any] = {}
        if checks[-1]["ok"]:
            gateway = _build_gateway_client(resolved_region, detail)
            try:
                info = await gateway.build_access_info()
                checks.append({"name": "dashboard_short_link", "ok": True, "dashboard_url": info.access_url})
                await gateway.connect()
                methods = gateway.methods
                checks.append({"name": "cookie_ws_handshake", "ok": True, "methods": len(methods)})
                snapshot = await gateway.channels_status(probe=False)
                config_snapshot = await gateway.config_get()
                config = config_snapshot.get("config") if isinstance(config_snapshot.get("config"), dict) else {}
            except Exception as exc:
                checks.append({"name": "gateway_connectivity", "ok": False, "error": str(exc)})
            finally:
                await gateway.close()

        channels_to_check = [normalized_channel] if normalized_channel else list(OPENCLAW_CHANNELS)
        plugin_entries = ((config.get("plugins") or {}).get("entries") or {}) if isinstance(config, dict) else {}
        for item in channels_to_check:
            selected_snapshot = _extract_channel_snapshot(snapshot, item)
            configured = _is_channel_configured(item, config=config, snapshot=selected_snapshot)
            plugin_id = str((OPENCLAW_CHANNEL_SPECS.get(item) or {}).get("plugin_id") or "")
            if item == "weixin":
                checks.append(
                    {
                        "name": "weixin_plugin_visible",
                        "ok": plugin_id in plugin_entries or selected_snapshot is not None,
                        "plugin_id": plugin_id,
                    }
                )
                checks.append(
                    _build_channel_doctor_availability_check(
                        name="weixin_status_snapshot",
                        available=selected_snapshot is not None,
                        configured=configured,
                        connect_required_message="首次连接前微信状态快照可能为空，执行 channel connect 后会自动补齐",
                    )
                )
                checks.append(
                    _build_channel_doctor_availability_check(
                        name="weixin_qr_rpc",
                        available="web.login.start" in methods and "web.login.wait" in methods,
                        configured=configured,
                        connect_required_message="首次连接会先自动启用 bundled weixin plugin，然后再暴露扫码 RPC",
                        connect_required_ok=False,
                        required_methods=["web.login.start", "web.login.wait"],
                    )
                )
            elif item == "feishu":
                checks.append(
                    {
                        "name": "feishu_plugin_visible",
                        "ok": plugin_id in plugin_entries or selected_snapshot is not None,
                        "plugin_id": plugin_id,
                    }
                )
                checks.append(
                    _build_channel_doctor_availability_check(
                        name="feishu_status_snapshot",
                        available=selected_snapshot is not None,
                        configured=configured,
                        connect_required_message="飞书尚未完成 connect/onboarding，首次接入前不会出现在 channel snapshot 中",
                    )
                )
                node_path = shutil.which("node")
                npx_path = shutil.which("npx")
                checks.append(
                    {
                        "name": "feishu_local_node",
                        "ok": bool(node_path and npx_path),
                        "node": node_path,
                        "npx": npx_path,
                    }
                )
            elif item == "agentspace":
                checks.append(
                    {
                        "name": "agentspace_plugin_visible",
                        "ok": plugin_id in plugin_entries or selected_snapshot is not None,
                        "plugin_id": plugin_id,
                    }
                )
                checks.append(
                    _build_channel_doctor_availability_check(
                        name="agentspace_status_snapshot",
                        available=selected_snapshot is not None,
                        configured=configured,
                        connect_required_message="Agentspace 尚未完成授权，首次 connect 前不会出现在 channel snapshot 中",
                    )
                )
                skills_cfg = config.get("skills") if isinstance(config.get("skills"), dict) else {}
                allow_bundled = skills_cfg.get("allowBundled") if isinstance(skills_cfg, dict) else []
                allow_list = skills_cfg.get("allow") if isinstance(skills_cfg, dict) else []
                visible_in_allow = AGENTSPACE_SKILL_NAME in allow_bundled if isinstance(allow_bundled, list) else False
                visible_in_allow = visible_in_allow or (
                    AGENTSPACE_SKILL_NAME in allow_list if isinstance(allow_list, list) else False
                )
                checks.append(
                    {
                        "name": "agentspace_skill_visible",
                        "ok": visible_in_allow,
                        "skill": AGENTSPACE_SKILL_NAME,
                    }
                )
                dep_check = _check_agentspace_local_deps()
                checks.append(
                    {
                        "name": "agentspace_local_deps",
                        **dep_check,
                    }
                )

        return {
            "ok": all(bool(item.get("ok")) for item in checks),
            "agent_id": detail.get("agent_id"),
            "name": detail.get("name"),
            "region": resolved_region,
            "channel": normalized_channel,
            "checks": checks,
            "snapshot": snapshot,
        }

    try:
        payload = asyncio.run(_run())
        _emit_data_payload("OpenClaw Channel Doctor", payload, subtitle=str(payload.get("name") or "-"))
    except Exception as e:
        _abort_openclaw_error(e, context="channel doctor 执行失败", argv=["openclaw", "channel", "doctor"])


@openclaw.command("deploy", context_settings=CONTEXT_SETTINGS)
@click.option("--name", "-n", default=None, help="OpenClaw 名称 (默认: openclaw-gateway)")
@click.option(
    "--region", "-r",
    default="cn-beijing-6",
    envvar="KSYUN_REGION",
    help="部署区域 (默认: cn-beijing-6)",
)
@click.option(
    "--security-profile",
    type=click.Choice(OPENCLAW_SECURITY_PROFILES, case_sensitive=False),
    default=None,
    help="安全预设: relaxed | strict | strictest (安全测试建议 strictest)",
)
@click.option("--strict-mode", "security_profile", flag_value="strict", help="快捷开启严格模式")
@click.option("--strictest", "security_profile", flag_value="strictest", help="快捷开启最严格安全模式")
@click.option(
    "--image",
    default=None,
    help="OpenClaw 镜像地址 (默认: 内置公共镜像；也可用 OPENCLAW_IMAGE/OPENCLAW_DOCKER_IMAGE)",
)
@click.option("--model-base-url", default=None, help="模型 Base URL (默认复用 OPENAI_BASE_URL)")
@click.option("--model-api-key", default=None, help="模型 API Key (可选；默认复用 OPENAI_API_KEY)")
@click.option("--default-model", default=None, help="默认模型名 (默认复用 OPENAI_MODEL_NAME)")
@dry_run_option("仅显示请求，不实际部署")
def deploy(
    name: Optional[str],
    region: str,
    security_profile: Optional[str],
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    dry_run: bool,
):
    """部署 OpenClaw 到云端

    \b
    通过 CreateAgentProduct (Container 模式) 部署预构建的 OpenClaw 镜像。
    模型配置自动复用 OPENAI_* 环境变量。

    \b
    示例:
        # 默认部署 (自动复用 .env 中的 OPENAI_* 变量)
        agentengine openclaw deploy
        # 显式开启严格模式
        agentengine openclaw deploy --strict-mode
        # 一键创建最严格实例（适合安全测试）
        agentengine openclaw deploy --security-profile strictest
        # 显式指定模型
        agentengine openclaw deploy --model-base-url https://api.example.com/v1 --model-api-key sk-xxx
        # 使用自定义镜像
        agentengine openclaw deploy --image hub.kce.ksyun.com/myns/openclaw:v2
    """
    dry_run = effective_dry_run(dry_run)
    try:
        run_async_with_dry_run(
            _deploy_openclaw(
                name=name,
                region=region,
                security_profile=security_profile,
                image=image,
                model_base_url=model_base_url,
                model_api_key=model_api_key,
                default_model=default_model,
                dry_run=dry_run,
            ),
            dry_run=dry_run,
        )
    except Exception as e:
        _abort_openclaw_error(e, context="部署失败", argv=["openclaw", "deploy"])


async def _deploy_openclaw(
    *,
    name: Optional[str],
    region: str,
    security_profile: Optional[str],
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    dry_run: bool,
):
    """异步部署 OpenClaw"""
    from ksadk.api import AgentEngineClient
    from ksadk.deployment.state import load_state, save_state, clear_state
    from dotenv import dotenv_values

    # 自动加载当前目录 .env（仅补充未导出的变量，不覆盖已导出的 shell 环境）
    project_dir = Path(".").resolve()
    env_file = project_dir / ".env"
    if env_file.exists():
        try:
            loaded = 0
            for k, v in dotenv_values(env_file).items():
                if not k or v is None:
                    continue
                if os.getenv(k) is None:
                    os.environ[k] = str(v)
                    loaded += 1
            if loaded:
                print_info(f"已从 .env 注入环境变量: {loaded} 项")
        except Exception as e:
            print_warn(f"读取 .env 失败，将继续使用当前 shell 环境: {e}")

    # 读取本地状态 (判断创建 vs 更新)
    state = load_state(project_dir)
    existing_agent_id = None
    if state.get("type") == "openclaw":
        existing_agent_id = state.get("agent_id")

    if name:
        openclaw_name = name
    else:
        openclaw_name = _generate_default_openclaw_name()
    resolved_image = image or _resolve_env("OPENCLAW_IMAGE", "OPENCLAW_DOCKER_IMAGE")
    bootstrap_cfg: Optional[Dict[str, Any]] = None
    if not resolved_image:
        bootstrap_cfg = await _fetch_bootstrap_config(region)
        server_default_image = _extract_bootstrap_image(bootstrap_cfg)
        if server_default_image:
            resolved_image = server_default_image
            print_info(f"未指定镜像，使用服务端默认镜像: {resolved_image}")
        _print_bootstrap_hints(bootstrap_cfg)
    image_ref = _resolve_image_ref(resolved_image)

    # 构建环境变量
    env_vars = _build_openclaw_env_vars(
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        default_model=default_model,
        security_profile=security_profile,
    )

    print_title("OpenClaw 云端部署", f"region: {region}")
    print_kv("名称", openclaw_name)
    print_kv("镜像", image_ref)
    print_kv("区域", region, value_style="#58a6ff")
    if security_profile:
        print_kv("安全预设", security_profile.lower())

    # 构建环境变量列表
    env_list = [
        {"Key": k, "Value": v, "IsSensitive": "KEY" in k or "TOKEN" in k or "SECRET" in k}
        for k, v in env_vars.items()
    ]
    # 资源规格（支持通过环境变量覆盖）
    cpu = _resolve_env("OPENCLAW_CPU") or "2"
    memory = _resolve_env("OPENCLAW_MEMORY") or "4Gi"

    # 构建请求数据
    request_data = {
        "name": openclaw_name,
        "description": "OpenClaw Gateway (managed by AgentEngine)",
        "framework": "openclaw",
        "artifact_type": "Container",
        "artifact_path": image_ref,
        "region": region,
        "resources": {"cpu": cpu, "memory": memory},
        "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 1000},
        "env_vars": env_list,
        # OpenClaw UI 需要浏览器直开；默认关闭平台层 ApiKey 保护，避免 dashboard 401
        "auth_type": "None",
        "inbound_identity_auth": "None",
    }

    # KCR 凭证：仅在显式提供用户名+密码时注入，避免公共镜像触发无效鉴权重试。
    image_credential = None
    kcr_username = _resolve_env("KCR_USERNAME", "KSYUN_ACCOUNT_ID")
    kcr_password = _resolve_env("KCR_PASSWORD")
    if kcr_username and kcr_password:
        image_credential = {
            "username": kcr_username,
            "password": kcr_password,
        }
        request_data["image_credential"] = image_credential
    elif kcr_password and not kcr_username:
        print_warn("检测到 KCR_PASSWORD 但缺少 KCR_USERNAME，已忽略镜像凭证")
    elif "/agentengine-public/" not in image_ref:
        print_warn("未配置 KCR_PASSWORD，私有镜像可能无法拉取 (公共镜像可忽略)")
        print_info("获取方式: https://kcr.console.ksyun.com/ → 访问凭证")

    if dry_run:
        async with AgentEngineClient(region=region, dry_run=True) as client:
            if existing_agent_id:
                update_payload = {
                    "artifact_type": "Container",
                    "artifact_path": image_ref,
                    "resources": {"cpu": cpu, "memory": memory},
                    "env_vars": env_list,
                    "auth_type": "None",
                    "inbound_identity_auth": "None",
                }
                if image_credential:
                    update_payload["image_credential"] = image_credential
                await client.update_agent(existing_agent_id, update_payload)
            else:
                await client.create_agent(request_data)
        return

    # 调用 API
    print_rule("部署 OpenClaw")
    try:
        latest_status = None
        updated_existing_agent = False
        async with AgentEngineClient(region=region) as client:
            if existing_agent_id:
                print_info(f"检测到本地状态: {existing_agent_id}，执行更新...")
                try:
                    update_payload = {
                        "artifact_type": "Container",
                        "artifact_path": image_ref,
                        "resources": {"cpu": cpu, "memory": memory},
                        "env_vars": env_list,
                        "auth_type": "None",
                        "inbound_identity_auth": "None",
                    }
                    if image_credential:
                        update_payload["image_credential"] = image_credential
                    res = await client.update_agent(existing_agent_id, update_payload)
                    agent_id = existing_agent_id
                    endpoint = res.get("endpoint") or state.get("endpoint")
                    api_key = state.get("api_key")
                    updated_existing_agent = True
                except Exception as update_err:
                    err_msg = str(update_err)
                    not_found = (
                        "code: 404" in err_msg.lower()
                        or "agent not found" in err_msg.lower()
                    )
                    if not not_found:
                        raise
                    print_warn(f"本地状态失效 ({existing_agent_id})，将自动回退为新建: {update_err}")
                    cleared = clear_state(project_dir, key=existing_agent_id)
                    if cleared:
                        print_info("已清理失效的 .agentengine.state")
                    existing_agent_id = None

            if not existing_agent_id:
                res = await client.create_agent(request_data)
                if not res:
                    raise Exception("Server 返回空响应，请查看 Server 日志")

                # CreateAgentProduct 返回 order_id，需要轮询获取 agent_id
                order_id = res.get("order_id")
                agent_id = res.get("agent_id")
                endpoint = res.get("endpoint")
                api_key = res.get("api_key")

                if order_id and not agent_id:
                    print_info(f"订单已创建: {order_id}，等待实例创建...")
                    import time
                    for i in range(12):  # 最多等 60s
                        time.sleep(5)
                        try:
                            detail = await client.get_agent(name=openclaw_name, include_api_key=True)
                            qa = detail.get("quick_access", {})
                            basic = detail.get("basic", {})
                            agent_id = basic.get("agent_id")
                            endpoint = qa.get("public_endpoint") or endpoint
                            api_key = qa.get("api_key") or api_key
                            if agent_id:
                                print_success(f"实例已创建: {agent_id}")
                                break
                        except Exception:
                            pass
                        print_info(f"等待中... ({(i+1)*5}s)")

                    if not agent_id:
                        print_warn("实例创建中，稍后使用 'agentengine openclaw list' 查看")

            # 保存状态
            saved_name = openclaw_name if not state.get("name") else state.get("name")
            if not existing_agent_id:
                saved_name = openclaw_name

            save_state(project_dir, {
                "type": "openclaw",
                "agent_id": agent_id,
                "name": saved_name,
                "region": region,
                "endpoint": endpoint,
                "api_key": api_key,
                "image": image_ref,
                "openclaw_auth_mode": env_vars.get("OPENCLAW_GATEWAY_AUTH_MODE"),
            })

            # 仅在更新已有实例时回读一次状态；新建时底层可能尚未落库，立即按 ID 查询会产生误导性报错。
            if updated_existing_agent and agent_id:
                try:
                    latest = await client.get_agent(agent_id=agent_id, include_api_key=False)
                    latest_status = str(((latest.get("basic") or {}).get("status") or "")).upper() or None
                except Exception:
                    latest_status = None

            print_success("OpenClaw 已提交部署")
            print_kv("Agent ID", agent_id or "(创建中)")
            if latest_status:
                print_kv("当前状态", latest_status)
            if endpoint:
                print_kv("Endpoint", endpoint, value_style="#58a6ff")
            if api_key:
                print_kv("API Key", api_key, value_style="#d29922")
            print_info("已保存状态到 .agentengine.state")
            print_info("建议先确认实例状态:")
            print_info("  agentengine openclaw status")
            if latest_status != "RUNNING":
                print_info("实例进入 RUNNING 后再打开 Dashboard:")
            else:
                print_info("可直接打开 Dashboard:")
            if agent_id:
                print_info(f"  agentengine dashboard {agent_id}")
            else:
                print_info("  agentengine dashboard")

    except DryRunExit:
        raise


@openclaw.command("list", context_settings=CONTEXT_SETTINGS)
@region_option()
@pagination_options(default_page=1, default_size=20)
@dry_run_option()
@cli_output_option()
def list_openclaws(region: str, page: int, size: int, dry_run: bool, output_mode: str | None):
    """列出已部署的 OpenClaw 实例"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    async def _list():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            resp = await client.list_agents(region=region, framework="openclaw", page=page, page_size=size)
            agents = resp.get("agents", []) or []
            total = int(resp.get("total") or len(agents))
            rows = []
            items = []
            for a in agents:
                status = (a.get("status") or "UNKNOWN").upper()
                rows.append(
                    (
                        str(a.get("agent_id", "-")),
                        str(a.get("name", "-")),
                        f"[{status_rich_style(status)}]{status}[/]",
                        str(a.get("endpoint", "N/A")),
                        str(a.get("region", "-")),
                    )
                )
                items.append(
                    {
                        "id": str(a.get("agent_id", "-")),
                        "name": str(a.get("name", "-")),
                        "status": status,
                        "endpoint": str(a.get("endpoint", "N/A")),
                        "region": str(a.get("region", "-")),
                    }
                )

            if not render_descriptor_list(
                OPENCLAW_RESOURCE,
                rows=rows,
                total=total,
                page=page,
                size=size,
                items=items,
            ):
                return

            account_summary = _summarize_openclaw_account(agents)
            region_summary = _summarize_openclaw_region(agents, region)
            print_info(f"账号: {account_summary}  region: {region_summary}  总计: {total}")

    try:
        run_async_with_dry_run(
            _list(),
            dry_run=dry_run,
            dry_run_resource="openclaw",
            dry_run_action="list",
        )
    except Exception as e:
        _abort_openclaw_error(e, context="获取列表失败", argv=["openclaw", "list"])


@openclaw.command("status", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False, default=None)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@dry_run_option()
@cli_output_option()
def status(agent_ref: Optional[str], region: Optional[str], dry_run: bool, output_mode: str | None):
    """查看 OpenClaw 状态

    \b
    AGENT_REF: Agent ID 或名称 (可选，默认从 .agentengine.state 读取)
    """
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    from ksadk.deployment.state import load_state

    state = load_state(Path(".").resolve())
    region = _resolve_region(region, state)
    resolved = resolve_openclaw_ref(agent_ref, cwd=Path(".").resolve(), include_state=True)

    # 无参数时从本地状态读取
    if not resolved:
        _abort_openclaw_error(
            resolution_error(
                OPENCLAW_RESOURCE.missing_ref_message or "请指定 OpenClaw。",
                hints=list(OPENCLAW_RESOURCE.resolution_commands),
            ),
            argv=["openclaw", "status"],
        )
        return
    agent_ref = resolved.value
    if resolved.source != "cli":
        print_info(f"未显式指定 OpenClaw，使用 {resolved.source_text}: {agent_ref}")
    from ksadk.api import AgentEngineClient

    async def _get():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            if agent_ref.startswith("ar-"):
                agent = await client.get_agent(agent_id=agent_ref)
            else:
                agent = await client.get_agent(name=agent_ref)

            if not agent:
                raise resolution_error(f"未找到 OpenClaw: {agent_ref}", hints=["agentengine openclaw list"])

            detail = _flatten_agent_detail(agent)
            status_val = detail.get("status", "UNKNOWN")
            created_at_display = _format_cli_timestamp(detail.get("created_at"))
            updated_at_display = _format_cli_timestamp(detail.get("updated_at"))
            render_descriptor_status(
                OPENCLAW_RESOURCE,
                subtitle=str(detail.get("name") or agent_ref),
                fields=[
                    ("ID", str(detail.get("agent_id") or "-"), None),
                    ("状态", str(status_val), status_rich_style(status_val)),
                    ("框架", str(detail.get("framework") or "-"), None),
                    ("区域", str(detail.get("region") or region), None),
                    ("Endpoint", str(detail.get("endpoint") or "N/A"), "#58a6ff"),
                    ("镜像", str(detail.get("artifact_path") or "-"), None),
                    ("创建时间", created_at_display, None),
                    ("更新时间", updated_at_display, None),
                ],
                item={
                    "id": str(detail.get("agent_id") or "-"),
                    "name": str(detail.get("name") or agent_ref),
                    "status": str(status_val),
                    "framework": str(detail.get("framework") or "-"),
                    "region": str(detail.get("region") or region),
                    "endpoint": str(detail.get("endpoint") or "N/A"),
                    "image": str(detail.get("artifact_path") or "-"),
                    "created_at": str(detail.get("created_at") or "-"),
                    "updated_at": str(detail.get("updated_at") or "-"),
                },
            )

    try:
        run_async_with_dry_run(
            _get(),
            dry_run=dry_run,
            dry_run_resource="openclaw",
            dry_run_action="status",
        )
    except Exception as e:
        _abort_openclaw_error(e, context="获取状态失败", argv=["openclaw", "status"])


def _delete_impl(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool):
    """删除 OpenClaw 实例。

    AGENT_REF: Agent ID
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    if not confirm_destructive(
        assume_yes=assume_yes,
        dry_run=dry_run,
        prompt=f"确定要删除这 {len(agent_refs)} 个 OpenClaw 实例吗?",
    ):
        return

    async def _delete():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            failed_refs: list[str] = []
            deleted_refs: list[str] = []
            for agent_ref in agent_refs:
                success = await client.delete_agent(agent_ref)
                if success:
                    deleted_refs.append(agent_ref)
                    print_success(f"OpenClaw 已删除: {agent_ref}")

                    # 清理本地状态
                    from ksadk.deployment.state import clear_state
                    try:
                        removed = clear_state(Path("."), key=agent_ref)
                        if removed:
                            print_info("本地状态文件已清理")
                        else:
                            print_warn("未清理本地状态文件: 当前目录状态与目标 ID 不匹配")
                    except Exception:
                        pass
                else:
                    failed_refs.append(agent_ref)

            if failed_refs:
                raise remote_error(
                    f"以下 OpenClaw 删除失败: {', '.join(failed_refs)}",
                    details={"deleted": deleted_refs, "failed": failed_refs},
                )
            return {
                "targets": list(agent_refs),
                "deleted": deleted_refs,
                "failed": failed_refs,
            }

    dry_run_kwargs = {"dry_run": dry_run}
    if is_json_output():
        dry_run_kwargs.update(
            dry_run_resource="openclaw",
            dry_run_action="delete",
        )
    try:
        result = run_async_with_dry_run(_delete(), **dry_run_kwargs)
    except Exception as e:
        _abort_openclaw_error(e, context="删除失败", argv=["openclaw", "delete"])
        return
    if result is not None:
        render_descriptor_status(
            OPENCLAW_RESOURCE,
            title="OpenClaw 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", ", ".join(result["deleted"]) or "-", None),
                ("失败", ", ".join(result["failed"]) or "-", None),
            ],
            action="delete",
            item=result,
        )


@openclaw.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_refs", nargs=-1, required=True)
@region_option()
@confirm_options()
@dry_run_option()
@cli_output_option()
def delete(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 OpenClaw 实例。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)


@openclaw.command("destroy", context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("agent_refs", nargs=-1, required=True)
@region_option()
@confirm_options()
@dry_run_option()
@cli_output_option()
def destroy(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 OpenClaw 实例。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)
