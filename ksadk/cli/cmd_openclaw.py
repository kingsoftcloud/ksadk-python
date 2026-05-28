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
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any

import click
from rich.measure import Measurement
from rich.table import Table as RichTable

from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import resolve_openclaw_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error
from ksadk.cli.network_options import build_network_payload, network_cli_kwargs, network_options
from ksadk.cli.storage import build_storage_config
from ksadk.cli.resource_common import ResourceActionDescriptor
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_dry_run_envelope,
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
from ksadk.deployment.agent_access import get_latest_agent_access
from ksadk.cli.model_catalog import fetch_provider_model_catalog, find_model_in_catalog
from ksadk.conversations.model_context import normalize_model_metadata
from ksadk.openclaw_gateway import OpenClawGatewayClient, OpenClawGatewayError, OpenClawGatewayRequestError
from ksadk.terminal_client import run_terminal_session

console = get_console()
# 默认 OpenClaw 镜像 (KCR 个人版)
DEFAULT_OPENCLAW_NAMESPACE = "agentengine-public"
DEFAULT_OPENCLAW_REPO = "openclaw"
DEFAULT_OPENCLAW_VERSION = "2026.5.22"
DEFAULT_OPENCLAW_REGISTRY = "ghcr.io"
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
OPENCLAW_CHANNELS = ("weixin", "feishu", "wps-xiezuo")
OPENCLAW_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WEIXIN_PLUGIN_ID = "openclaw-weixin"
FEISHU_PLUGIN_ID = "openclaw-lark"
FEISHU_CHANNEL_KEY = "feishu"
WPS_XIEZUO_PLUGIN_ID = "wps-xiezuo"
WPS_XIEZUO_CHANNEL_KEY = "wps-xiezuo"
WPS_XIEZUO_DEFAULT_ACCOUNT_ID = "default"
WEIXIN_REMOTE_LOGIN_ARGV = ["openclaw", "channels", "login", "--channel", WEIXIN_PLUGIN_ID]
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
    "wps-xiezuo": {
        "plugin_id": WPS_XIEZUO_PLUGIN_ID,
        "channel_key": WPS_XIEZUO_CHANNEL_KEY,
        "default_account_id": WPS_XIEZUO_DEFAULT_ACCOUNT_ID,
    },
}
OPENCLAW_CHANNEL_CONNECT_HELP = """连接指定 channel。

\b
不同 channel 的接入方式不同：
  微信：扫码登录。
    agentengine openclaw channel connect <id> --channel weixin
  飞书：启动官方 onboarding 流程。
    agentengine openclaw channel connect <id> --channel feishu
  WPS 协作：写入开放平台 appId/appSecret 并启动长连接。
    agentengine openclaw channel connect <id> --channel wps-xiezuo --app-id <appId> --app-secret <appSecret>

\b
WPS 协作说明：
  --app-id / --app-secret 是让 channel 可连接可用的必需凭证。
  --dm-policy=open 表示允许所有用户私聊；pairing 表示未知用户需要配对审批。
  --account-id 当前仅支持 default。
"""
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
            name="tui",
            canonical_command="agentengine openclaw tui [openclaw_ref]",
            help_text="连接远端 OpenClaw 原生 TUI",
            kind="interactive",
            supports_output=True,
            supports_dry_run=True,
        ),
        ResourceActionDescriptor(
            name="repair",
            canonical_command="agentengine openclaw repair [openclaw_ref]",
            help_text="通过控制面执行 OpenClaw 修复动作",
            kind="write",
            supports_output=True,
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
        next_steps=(
            "agentengine invoke <id>",
            "agentengine openclaw tui <id>",
            "agentengine dashboard open <id> --path /chat",
        ),
    ),
    examples=(
        "agentengine openclaw deploy",
        "agentengine openclaw list",
        "agentengine openclaw status <id>",
        "agentengine openclaw tui <id>",
        "agentengine openclaw gateway open <id>",
        "agentengine openclaw repair <id>",
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


def _load_openclaw_project_name(project_dir: Path) -> Optional[str]:
    """从 OpenClaw 项目配置读取 init 时指定的项目名。"""
    for file_name in ("agentengine.yaml", "ksadk.yaml"):
        config_path = project_dir / file_name
        if not config_path.exists():
            continue
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        framework = str(data.get("framework") or "").strip().lower()
        if framework != "openclaw":
            continue
        project_name = str(data.get("name") or "").strip()
        if project_name:
            return project_name
    return None


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


def _strip_provider_prefix(provider_id: str, model_id: str) -> str:
    provider = str(provider_id or "").strip()
    model = str(model_id or "").strip()
    if provider and model.lower().startswith(f"{provider.lower()}/"):
        return model.split("/", 1)[1].strip()
    return model


def _default_openclaw_model_inputs(provider_id: str, model_id: str) -> list[str]:
    if str(provider_id or "").strip().lower() == "ksyun" and str(model_id or "").strip().lower() == "glm-5.1":
        return ["text"]
    return ["text", "image"]


def _openclaw_catalog_inputs(
    *,
    provider_id: str,
    model_id: str,
    metadata: Dict[str, Any],
) -> list[str]:
    raw = metadata.get("_provider_raw_model")
    if isinstance(raw, dict):
        architecture = raw.get("architecture")
        if isinstance(architecture, dict):
            modalities = architecture.get("input_modalities")
            if isinstance(modalities, list):
                normalized = {
                    str(item or "").strip().lower()
                    for item in modalities
                    if str(item or "").strip()
                }
                result = ["text"]
                if normalized & {"image", "图片", "图像"}:
                    result.append("image")
                return result

    raw_capabilities = raw.get("capabilities") if isinstance(raw, dict) else None
    capabilities = raw_capabilities if isinstance(raw_capabilities, dict) else None
    if not capabilities and isinstance(metadata.get("capabilities"), dict):
        metadata_capabilities = metadata["capabilities"]
        if metadata_capabilities.get("multimodal_input_image"):
            capabilities = metadata_capabilities
    if isinstance(capabilities, dict):
        result = ["text"]
        if capabilities.get("multimodal_input_image"):
            result.append("image")
        return result
    return _default_openclaw_model_inputs(provider_id, model_id)


def _openclaw_catalog_item_from_metadata(
    raw_model: Dict[str, Any],
    *,
    provider_id: str,
    provider_api: str,
) -> Optional[Dict[str, Any]]:
    metadata = normalize_model_metadata(raw_model)
    model_id = _strip_provider_prefix(
        provider_id,
        str(metadata.get("id") or metadata.get("name") or "").strip(),
    )
    if not model_id:
        return None

    inputs = _openclaw_catalog_inputs(
        provider_id=provider_id,
        model_id=model_id,
        metadata=metadata,
    )
    return {
        "id": model_id,
        "name": str(raw_model.get("name") or metadata.get("display_name") or model_id),
        "api": str(raw_model.get("api") or provider_api or "openai-completions"),
        "reasoning": bool(raw_model.get("reasoning", True)),
        "input": inputs or _default_openclaw_model_inputs(provider_id, model_id),
        "cost": raw_model.get("cost")
        if isinstance(raw_model.get("cost"), dict)
        else {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": int(metadata.get("context_window_tokens") or 200_000),
        "maxTokens": int(metadata.get("max_output_tokens") or 20_000),
    }


def _apply_openclaw_provider_model_catalog(
    env: Dict[str, str],
    raw_models: list[Any],
) -> bool:
    if not raw_models or str(env.get("OPENCLAW_MODEL_CATALOG_JSON") or "").strip():
        return False

    provider_id = str(env.get("OPENCLAW_MODEL_PROVIDER_ID") or "ksyun").strip() or "ksyun"
    provider_api = str(env.get("OPENCLAW_MODEL_API") or "openai-completions").strip() or "openai-completions"
    catalog: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raw_model = {"id": str(raw_model or "").strip()}
        item = _openclaw_catalog_item_from_metadata(
            raw_model,
            provider_id=provider_id,
            provider_api=provider_api,
        )
        if not item:
            continue
        model_key = str(item["id"])
        if model_key in seen:
            continue
        seen.add(model_key)
        catalog.append(item)

    if not catalog:
        return False
    env["OPENCLAW_MODEL_CATALOG_JSON"] = json.dumps(
        catalog,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return True


def _apply_openclaw_provider_model_metadata(
    env: Dict[str, str],
    raw_model: Dict[str, Any],
) -> bool:
    return _apply_openclaw_provider_model_catalog(env, [raw_model])


def _openclaw_requested_model_ids(env: Dict[str, str]) -> list[str]:
    raw_allowlist = str(
        env.get("OPENCLAW_MODEL_ALLOWLIST")
        or env.get("AGENTENGINE_MODEL_ALLOWLIST")
        or ""
    ).strip()
    if raw_allowlist:
        return [
            item.strip()
            for item in raw_allowlist.replace(";", ",").split(",")
            if item.strip()
        ]
    primary_model = str(
        env.get("OPENCLAW_DEFAULT_MODEL")
        or env.get("OPENAI_MODEL_NAME")
        or ""
    ).strip()
    return [primary_model] if primary_model else []


def _filter_openclaw_provider_catalog(
    env: Dict[str, str],
    provider_catalog: list[Any],
) -> list[Any]:
    requested_models = _openclaw_requested_model_ids(env)
    if not requested_models:
        return []
    raw_models = [
        item.get("_provider_raw_model") or item
        if isinstance(item, dict)
        else item
        for item in provider_catalog
    ]
    selected: list[Any] = []
    seen: set[str] = set()
    for model_id in requested_models:
        match = find_model_in_catalog(raw_models, model_id)
        if match is None:
            continue
        identities = sorted(str(value).strip().lower() for value in (model_id, str(match)) if str(value).strip())
        dedupe_key = identities[0] if identities else str(model_id).lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        selected.append(match)
    return selected


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
    default_model_base_url = "https://kspmas.ksyun.com/v1/"
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
    model = model_preference or "glm-5.1"
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
    gateway_auth_mode = _resolve_env("OPENCLAW_GATEWAY_AUTH_MODE")
    gateway_token = _resolve_env("OPENCLAW_GATEWAY_TOKEN")
    gateway_password = _resolve_env("OPENCLAW_GATEWAY_PASSWORD")

    env["OPENCLAW_GATEWAY_BIND"] = "lan"
    if gateway_auth_mode:
        env["OPENCLAW_GATEWAY_AUTH_MODE"] = gateway_auth_mode
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
    openclaw_model_allowlist = _resolve_env("OPENCLAW_MODEL_ALLOWLIST")
    agentengine_model_allowlist = _resolve_env("AGENTENGINE_MODEL_ALLOWLIST")
    if openclaw_model_allowlist:
        env["OPENCLAW_MODEL_ALLOWLIST"] = openclaw_model_allowlist
    elif agentengine_model_allowlist:
        env["AGENTENGINE_MODEL_ALLOWLIST"] = agentengine_model_allowlist
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
    if gateway_token:
        env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
    if gateway_password:
        env["OPENCLAW_GATEWAY_PASSWORD"] = gateway_password
    for passthrough_key in [
        "OPENCLAW_CHANNEL_BOOTSTRAP_JSON",
        "OPENCLAW_BROWSER_SSRF_POLICY_JSON",
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

    return _normalize_openclaw_gateway_auth_env(env)


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


def _normalize_openclaw_gateway_auth_env(env: dict[str, str]) -> dict[str, str]:
    """标准化 OpenClaw gateway 鉴权模式与共享密钥配置。"""
    normalized_env = dict(env or {})
    raw_mode = str(normalized_env.get("OPENCLAW_GATEWAY_AUTH_MODE") or "").strip().lower()
    raw_token = str(normalized_env.get("OPENCLAW_GATEWAY_TOKEN") or "").strip()
    raw_password = str(normalized_env.get("OPENCLAW_GATEWAY_PASSWORD") or "").strip()

    if raw_mode and raw_mode not in {"trusted-proxy", "token", "none"}:
        raise ValueError("OPENCLAW_GATEWAY_AUTH_MODE 仅支持 trusted-proxy、token 或 none")

    auth_mode = raw_mode or ("token" if raw_token or raw_password else "trusted-proxy")
    if auth_mode == "token":
        if raw_token and raw_password and raw_token != raw_password:
            raise ValueError("OPENCLAW_GATEWAY_TOKEN 与 OPENCLAW_GATEWAY_PASSWORD 同时提供时必须一致")
        shared_secret = raw_token or raw_password
        if not shared_secret:
            raise ValueError(
                "OPENCLAW_GATEWAY_AUTH_MODE=token 时必须提供 OPENCLAW_GATEWAY_TOKEN 或 OPENCLAW_GATEWAY_PASSWORD"
            )
        normalized_env["OPENCLAW_GATEWAY_AUTH_MODE"] = "token"
        normalized_env["OPENCLAW_GATEWAY_TOKEN"] = shared_secret
        normalized_env["OPENCLAW_GATEWAY_PASSWORD"] = shared_secret
        return normalized_env

    if raw_token or raw_password:
        raise ValueError(
            "仅在 OPENCLAW_GATEWAY_AUTH_MODE=token 时支持 OPENCLAW_GATEWAY_TOKEN 或 OPENCLAW_GATEWAY_PASSWORD"
        )

    normalized_env["OPENCLAW_GATEWAY_AUTH_MODE"] = auth_mode
    normalized_env.pop("OPENCLAW_GATEWAY_TOKEN", None)
    normalized_env.pop("OPENCLAW_GATEWAY_PASSWORD", None)
    return normalized_env


def _parse_extra_openclaw_env_pairs(items: tuple[str, ...] | list[str] | None) -> dict[str, str]:
    """解析 deploy --env 传入的自定义环境变量。"""
    parsed: dict[str, str] = {}
    for raw_item in items or ():
        item = str(raw_item or "").strip()
        if not item or "=" not in item:
            raise ValueError(f"自定义环境变量格式错误: {raw_item!r}，应为 KEY=VALUE")
        key, value = item.split("=", 1)
        key = key.strip()
        if not OPENCLAW_ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"自定义环境变量名不合法: {key!r}，请使用合法的环境变量名")
        if key == "OPENCLAW_GATEWAY_AUTH_MODE":
            normalized = value.strip().lower()
            if normalized not in {"trusted-proxy", "token", "none"}:
                raise ValueError("OPENCLAW_GATEWAY_AUTH_MODE 仅支持 trusted-proxy、token 或 none")
            value = normalized
        parsed[key] = value
    return parsed


def _build_openclaw_memory_config(
    *,
    memory_system: str | None,
    mem0_instance_id: str | None,
    mem0_instance_name: str | None,
    mem0_region: str | None,
) -> dict[str, str] | None:
    normalized_system = str(memory_system or "").strip().lower()
    normalized_mem0_instance_id = str(mem0_instance_id or "").strip()
    normalized_mem0_instance_name = str(mem0_instance_name or "").strip()
    normalized_mem0_region = str(mem0_region or "").strip()

    has_mem0_detail = any(
        [
            normalized_mem0_instance_id,
            normalized_mem0_instance_name,
            normalized_mem0_region,
        ]
    )
    if not normalized_system and has_mem0_detail:
        normalized_system = "mem0"
    if not normalized_system:
        return None

    if normalized_system == "openclaw_default":
        if has_mem0_detail:
            raise click.UsageError("使用 --memory-system openclaw_default 时不能再传入 mem0 参数。")
        return {"memory_system": "openclaw_default"}

    if normalized_system == "mem0":
        if not normalized_mem0_instance_id:
            raise click.UsageError("使用 --memory-system mem0 时必须传入 --mem0-instance-id。")
        payload = {
            "memory_system": "mem0",
            "mem0_instance_id": normalized_mem0_instance_id,
        }
        if normalized_mem0_instance_name:
            payload["mem0_instance_name"] = normalized_mem0_instance_name
        if normalized_mem0_region:
            payload["mem0_region"] = normalized_mem0_region
        return payload

    raise click.UsageError(f"不支持的记忆后端: {normalized_system}")


def _parse_image(image: Optional[str]) -> tuple[str, str, str]:
    """解析镜像地址为 (namespace, repo, version)

    支持格式:
    - ghcr.io/ns/repo:tag → (ns, repo, tag)
    - ns/repo:tag → (ns, repo, tag)
    - 无输入 → 使用默认值
    """
    if not image:
        return DEFAULT_OPENCLAW_NAMESPACE, DEFAULT_OPENCLAW_REPO, DEFAULT_OPENCLAW_VERSION

    # 去掉 registry 域名前缀 (ghcr.io/)
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
        "framework": basic.get("framework") or deploy.get("framework") or agent.get("framework") or "",
        "region": basic.get("region") or deploy.get("region") or agent.get("region") or "",
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint") or "",
        "artifact_path": deploy.get("artifact_path") or agent.get("artifact_path") or "",
        "created_at": basic.get("created_at") or agent.get("created_at") or "",
        "updated_at": basic.get("updated_at") or agent.get("updated_at") or "",
        "api_key": quick.get("api_key") or agent.get("api_key"),
        "langfuse_url": (agent.get("advanced") or {}).get("observability_url") or agent.get("langfuse_trace_url") or "",
    }


async def _get_openclaw_detail_with_client(
    client,
    agent_ref: str,
    *,
    include_api_key: bool = False,
) -> dict[str, Any]:
    if str(agent_ref).startswith("ar-"):
        agent = await client.get_agent(agent_id=agent_ref, include_api_key=include_api_key)
    else:
        agent = await client.get_agent(name=agent_ref, include_api_key=include_api_key)
    detail = _flatten_agent_detail(agent)
    framework = str(detail.get("framework") or "").strip().lower()
    if framework and framework != "openclaw":
        raise resolution_error(f"目标 Agent 不是 OpenClaw: {agent_ref}", hints=["agentengine openclaw list"])
    return detail


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
        detail = await _get_openclaw_detail_with_client(client, resolved.value)

    if not detail:
        raise resolution_error(f"未找到 OpenClaw: {resolved.value}", hints=["agentengine openclaw list"])
    return resolved_region, detail


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


def _render_openclaw_dry_run(action: str, request: dict[str, Any], hints: tuple[str, ...] = ()) -> None:
    if is_json_output():
        emit_json(
            build_dry_run_envelope(
                resource="openclaw",
                action=action,
                request=request,
                hints=list(hints),
            )
        )
        return
    print_title("OpenClaw Dry Run", f"action: {action}")
    for key, value in request.items():
        if isinstance(value, (list, tuple)):
            rendered = " ".join(str(item) for item in value) or "-"
        elif isinstance(value, dict):
            rendered = json_dumps(value)
        else:
            rendered = str(value if value is not None else "-")
        print_kv(key, rendered)
    for hint in hints:
        print_info(hint)


def _build_openclaw_tui_options(
    *,
    message: str | None = None,
    thinking: str | None = None,
    history_limit: int | None = None,
    timeout_ms: int | None = None,
    deliver: bool = False,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if message:
        options["message"] = message
    if thinking:
        options["thinking"] = thinking
    if history_limit is not None:
        options["history_limit"] = int(history_limit)
    if timeout_ms is not None:
        options["timeout_ms"] = int(timeout_ms)
    if deliver:
        options["deliver"] = True
    return options


def _select_openclaw_gateway_secret(
    *,
    gateway_token: str | None = None,
    gateway_password: str | None = None,
    state: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    state = state or {}
    detail = detail or {}
    token = str(
        gateway_token
        or os.getenv("OPENCLAW_GATEWAY_TOKEN")
        or detail.get("openclaw_gateway_token")
        or state.get("openclaw_gateway_token")
        or ""
    ).strip()
    password = str(
        gateway_password
        or os.getenv("OPENCLAW_GATEWAY_PASSWORD")
        or detail.get("openclaw_gateway_password")
        or state.get("openclaw_gateway_password")
        or ""
    ).strip()
    if token and password and token != password:
        raise click.ClickException("OPENCLAW_GATEWAY_TOKEN 与 OPENCLAW_GATEWAY_PASSWORD 同时提供时必须一致")
    if token:
        return "token", token
    if password:
        return "password", password
    return None, None


def _mask_openclaw_secret(secret: str | None) -> str:
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= 4:
        return "****"
    return f"{text[:4]}****"


def _openclaw_auth_mode_from_sources(detail: dict[str, Any], state: dict[str, Any] | None = None) -> str:
    return str(
        detail.get("openclaw_auth_mode")
        or detail.get("gateway_auth_mode")
        or (state or {}).get("openclaw_auth_mode")
        or (state or {}).get("gateway_auth_mode")
        or ""
    ).strip().lower()


def _openclaw_state_gateway_token(env_vars: dict[str, str]) -> str | None:
    """返回需要持久化到本地 state 的 OpenClaw Gateway token。"""
    if str(env_vars.get("OPENCLAW_GATEWAY_AUTH_MODE") or "").strip().lower() != "token":
        return None
    token = str(
        env_vars.get("OPENCLAW_GATEWAY_TOKEN")
        or env_vars.get("OPENCLAW_GATEWAY_PASSWORD")
        or ""
    ).strip()
    return token or None


def _openclaw_runtime_endpoint(detail: dict[str, Any]) -> str:
    return str(detail.get("endpoint") or "").strip()


def _openclaw_terminal_api_key(detail: dict[str, Any]) -> str | None:
    return str(detail.get("api_key") or "").strip() or None


def _is_weixin_web_login_unavailable(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "web login provider is not available" in message
        or "method not found" in message
        or "unknown method" in message
        or "unsupported method" in message
    )


async def _run_weixin_remote_cli_login(
    region: str,
    detail: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    endpoint = _openclaw_runtime_endpoint(detail)
    if not endpoint:
        raise OpenClawGatewayError("OpenClaw runtime endpoint 为空，无法通过远端 CLI 执行微信登录")

    print_info(f"当前 OpenClaw 未暴露微信 web login RPC，改用远端 OpenClaw CLI 登录流程（{reason}）")
    exit_code = await run_terminal_session(
        endpoint=endpoint,
        api_key=_openclaw_terminal_api_key(detail),
        mode="exec",
        argv=WEIXIN_REMOTE_LOGIN_ARGV,
    )
    if exit_code:
        raise OpenClawGatewayError(f"微信远端登录流程执行失败，exit_code={exit_code}")

    snapshot = await _fetch_channel_snapshot_with_retry(
        region,
        detail,
        probe=True,
    )
    return {
        "ok": True,
        "agent_id": detail.get("agent_id"),
        "name": detail.get("name"),
        "region": region,
        "channel": "weixin",
        "mode": "remote_cli",
        "reason": reason,
        "argv": list(WEIXIN_REMOTE_LOGIN_ARGV),
        "status": _extract_channel_snapshot(snapshot, "weixin"),
    }


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


def _is_gateway_reload_disconnect_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "websocket receive failed" in message
        and ("1011" in message or "bad gateway" in message or "going away" in message)
    )


async def _config_apply_and_wait_for_reload(
    gateway: OpenClawGatewayClient,
    region: str,
    detail: dict[str, Any],
    *,
    config: dict[str, Any],
    base_hash: str,
    note: str,
) -> None:
    try:
        await gateway.config_apply(config=config, base_hash=base_hash, note=note)
    except OpenClawGatewayRequestError:
        raise
    except OpenClawGatewayError as exc:
        if not _is_gateway_reload_disconnect_error(exc):
            raise
        print_warn("配置已提交，gateway reload 期间连接短暂中断；等待恢复后继续验证")
        await _wait_for_gateway_ready(region, detail, timeout_seconds=90.0)
        return
    await _wait_for_gateway_reload_after_config_apply(gateway, region, detail)


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
    if channel == "wps-xiezuo":
        return bool(str(channel_cfg.get("appId") or "").strip() and str(channel_cfg.get("appSecret") or "").strip())
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


def _mutate_wps_xiezuo_connect_config(
    config: dict[str, Any],
    *,
    app_id: str,
    app_secret: str,
    account_id: str,
    agent_id: str,
    dm_policy: str,
    group_policy: str,
    base_url: str,
) -> bool:
    changed = _ensure_plugin_enabled(config, WPS_XIEZUO_PLUGIN_ID)
    channels = config.setdefault("channels", {})
    channel_cfg = channels.setdefault(WPS_XIEZUO_CHANNEL_KEY, {})
    channel_cfg.pop("accounts", None)
    channel_cfg.pop("defaultAccountId", None)
    desired_channel = {
        "enabled": True,
        "appId": app_id,
        "appSecret": app_secret,
        "baseUrl": base_url,
        "sdk": {
            "enabled": True,
            "logLevel": "info",
        },
        "dmPolicy": dm_policy,
        "allowFrom": ["*"] if dm_policy == "open" else [],
        "groupPolicy": group_policy,
        "instantAck": {
            "enabled": True,
            "text": "内容处理中，请稍候...",
        },
        "mcp": {
            "enabled": True,
            "mode": "app",
        },
    }
    for key, value in desired_channel.items():
        if channel_cfg.get(key) != value:
            channel_cfg[key] = value
            changed = True
    session_cfg = config.setdefault("session", {})
    if session_cfg.get("dmScope") != "per-account-channel-peer":
        session_cfg["dmScope"] = "per-account-channel-peer"
        changed = True
    bindings = config.setdefault("bindings", [])
    if isinstance(bindings, list):
        next_bindings = [
            item for item in bindings
            if not (isinstance(item, dict) and item.get("match", {}).get("channel") == WPS_XIEZUO_CHANNEL_KEY)
        ]
        desired_binding = {
            "type": "route",
            "agentId": agent_id,
            "match": {
                "channel": WPS_XIEZUO_CHANNEL_KEY,
            },
        }
        next_bindings.append(desired_binding)
        if next_bindings != bindings:
            config["bindings"] = next_bindings
            changed = True
    return changed


def _mutate_wps_xiezuo_enabled(
    config: dict[str, Any],
    *,
    enabled: bool,
    account_id: Optional[str],
) -> bool:
    normalized_account = str(account_id or WPS_XIEZUO_DEFAULT_ACCOUNT_ID).strip()
    if normalized_account and normalized_account != WPS_XIEZUO_DEFAULT_ACCOUNT_ID:
        raise click.ClickException("WPS 协作插件使用扁平 channel 配置，`--account-id` 仅支持 default")
    changed = _ensure_plugin_enabled(config, WPS_XIEZUO_PLUGIN_ID) if enabled else False
    channels = config.setdefault("channels", {})
    channel_cfg = channels.setdefault(WPS_XIEZUO_CHANNEL_KEY, {})
    channel_cfg.pop("accounts", None)
    channel_cfg.pop("defaultAccountId", None)
    if channel_cfg.get("enabled") is not enabled:
        channel_cfg["enabled"] = enabled
        changed = True
    return changed


def _check_wps_xiezuo_local_deps() -> dict[str, Any]:
    node_path = shutil.which("node")
    npm_path = shutil.which("npm")
    return {
        "ok": bool(node_path and npm_path),
        "node": node_path,
        "npm": npm_path,
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


async def _run_openclaw_repair_action(
    agent_ref: Optional[str],
    *,
    region: Optional[str],
    repair_action: str = "doctor-fix",
) -> dict[str, Any]:
    from ksadk.api import AgentEngineClient

    resolved_region, detail = await _resolve_openclaw_detail_or_raise(agent_ref, region=region)
    async with AgentEngineClient(region=resolved_region) as client:
        repair_payload = await client.run_openclaw_repair(
            str(detail.get("agent_id") or ""),
            repair_action=repair_action,
        )

    return {
        "ok": bool(repair_payload.get("ok", False)),
        "agent_id": detail.get("agent_id"),
        "name": detail.get("name"),
        "region": resolved_region,
        "current_status": detail.get("status"),
        **repair_payload,
    }


@openclaw.command("tui", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--endpoint", "-e", default=None, help="OpenClaw Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (trusted-proxy/none 场景可用)")
@click.option(
    "--gateway-token",
    envvar="OPENCLAW_GATEWAY_TOKEN",
    default=None,
    help="OpenClaw Gateway token（token 模式必需，不是 ak-*）",
)
@click.option(
    "--gateway-password",
    envvar="OPENCLAW_GATEWAY_PASSWORD",
    default=None,
    help="OpenClaw Gateway password（password 模式必需）",
)
@click.option("--session", "-s", default=None, help="OpenClaw Session key")
@click.option("--message", "-m", default=None, help="连接后发送的初始消息")
@click.option("--thinking", default=None, help="Thinking level override")
@click.option("--history-limit", type=click.IntRange(1, 10000), default=None, help="历史条数 (默认使用 OpenClaw 默认值)")
@click.option("--timeout-ms", type=click.IntRange(1, 86400000), default=None, help="Agent timeout ms")
@click.option("--deliver", is_flag=True, help="Deliver assistant replies")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def tui_openclaw(
    agent_ref: Optional[str],
    region: Optional[str],
    endpoint: Optional[str],
    api_key: Optional[str],
    gateway_token: Optional[str],
    gateway_password: Optional[str],
    session: Optional[str],
    message: Optional[str],
    thinking: Optional[str],
    history_limit: Optional[int],
    timeout_ms: Optional[int],
    deliver: bool,
    insecure: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """连接远端 OpenClaw 原生 TUI（不需要本机安装 OpenClaw CLI）。

    该命令不依赖本机安装 OpenClaw CLI；AgentEngine 会连接远端 runtime proxy，
    由容器内的 OpenClaw CLI 执行 `openclaw tui`。
    """
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    options = _build_openclaw_tui_options(
        message=message,
        thinking=thinking,
        history_limit=history_limit,
        timeout_ms=timeout_ms,
        deliver=deliver,
    )
    if dry_run:
        secret_kind, gateway_secret = _select_openclaw_gateway_secret(
            gateway_token=gateway_token,
            gateway_password=gateway_password,
        )
        _render_openclaw_dry_run(
            "tui",
            {
                "agent_ref": agent_ref,
                "endpoint": endpoint,
                "mode": "tui",
                "session": session,
                "insecure": insecure,
                "gateway_auth_kind": secret_kind,
                "gateway_token_provided": bool(gateway_secret),
                "options": options,
            },
            hints=("dry-run 未解析远端 OpenClaw，也未建立 websocket。",),
        )
        return

    from ksadk.deployment.state import load_state

    state = load_state(Path(".").resolve())
    resolved_region = _resolve_region(region, state)
    detail: dict[str, Any] = dict(state or {})
    if endpoint:
        resolved_endpoint = endpoint
    elif not agent_ref and str(state.get("endpoint") or "").strip():
        resolved_endpoint = str(state.get("endpoint") or "").strip()
    else:
        resolved_region, detail = asyncio.run(_resolve_openclaw_detail_or_raise(agent_ref, region=region))
        resolved_endpoint = str(detail.get("endpoint") or "").strip()
    if not resolved_endpoint:
        raise click.ClickException("未解析到 OpenClaw Endpoint，请传入 --endpoint 或指定 OpenClaw ID/名称")

    secret_kind, gateway_secret = _select_openclaw_gateway_secret(
        gateway_token=gateway_token,
        gateway_password=gateway_password,
        state=state,
        detail=detail,
    )
    auth_mode = _openclaw_auth_mode_from_sources(detail, state)
    if auth_mode in {"token", "password"} and not gateway_secret:
        raise click.ClickException(
            "当前 OpenClaw Gateway 为 token/password 模式，agentengine openclaw tui 需要 OpenClaw Gateway token/password。\n"
            "请使用: agentengine openclaw tui --gateway-token <token>\n"
            "或设置: OPENCLAW_GATEWAY_TOKEN=<token> agentengine openclaw tui\n"
            "如果部署时传过 OPENCLAW_GATEWAY_TOKEN，请重新运行部署让本地 .agentengine.state 记录该 token。\n"
            "注意：这里不是 AgentEngine API Key（ak-*）。"
        )

    terminal_api_key = gateway_secret or api_key or str(detail.get("api_key") or state.get("api_key") or "").strip() or None
    click.secho("🖥️  OpenClaw Native Remote TUI", fg="blue", bold=True)
    click.echo(f"   Endpoint: {resolved_endpoint}")
    if gateway_secret:
        click.echo(f"   Runtime Auth: OpenClaw Gateway {secret_kind} {_mask_openclaw_secret(gateway_secret)}")
    elif terminal_api_key:
        click.echo(f"   Runtime Auth: Bearer {_mask_openclaw_secret(terminal_api_key)}")
    else:
        click.echo("   Runtime Auth: anonymous/trusted-proxy")
    click.echo("   退出: Ctrl-D 或 Ctrl-C")
    try:
        exit_code = asyncio.run(
            run_terminal_session(
                endpoint=resolved_endpoint,
                api_key=terminal_api_key,
                session_id=session,
                insecure=insecure,
                mode="tui",
                argv=[],
                options=options,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except Exception as exc:
        click.secho(f"\n❌ OpenClaw 终端连接失败: {exc}", fg="red")
        click.echo("   浏览器聊天页请改用: agentengine dashboard open --path /chat")
        raise SystemExit(1)
    if exit_code:
        raise SystemExit(exit_code)


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
@click.option("--fix", "do_fix", is_flag=True, help="不走 gateway 内诊断，改为通过控制面执行 openclaw doctor --fix")
@cli_output_option()
def gateway_doctor(
    agent_ref: Optional[str],
    region: Optional[str],
    do_fix: bool,
    output_mode: str | None,
):
    """检查 gateway 短链、cookie 与 websocket 链路。"""
    _ = output_mode

    async def _run() -> dict[str, Any]:
        if do_fix:
            return await _run_openclaw_repair_action(
                agent_ref,
                region=region,
                repair_action="doctor-fix",
            )
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
        title = "OpenClaw Gateway Doctor Fix" if do_fix else "OpenClaw Gateway Doctor"
        _emit_data_payload(title, payload, subtitle=str(payload.get("name") or "-"))
    except Exception as e:
        argv = ["openclaw", "gateway", "doctor"]
        if do_fix:
            argv.append("--fix")
        _abort_openclaw_error(e, context="gateway doctor 执行失败", argv=argv)


@openclaw.command("repair", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option(
    "--action",
    "repair_action",
    type=click.Choice(["doctor-fix"], case_sensitive=False),
    default="doctor-fix",
    show_default=True,
    help="控制面修复动作",
)
@cli_output_option()
def openclaw_repair(
    agent_ref: Optional[str],
    region: Optional[str],
    repair_action: str,
    output_mode: str | None,
):
    """通过控制面执行 OpenClaw 修复动作。"""
    _ = output_mode

    async def _run() -> dict[str, Any]:
        return await _run_openclaw_repair_action(
            agent_ref,
            region=region,
            repair_action=str(repair_action or "doctor-fix").strip().lower(),
        )

    try:
        payload = asyncio.run(_run())
        _emit_data_payload("OpenClaw Repair", payload, subtitle=str(payload.get("name") or "-"))
    except Exception as e:
        _abort_openclaw_error(e, context="OpenClaw repair 执行失败", argv=["openclaw", "repair"])


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


@openclaw_channel.command("connect", context_settings=CONTEXT_SETTINGS, help=OPENCLAW_CHANNEL_CONNECT_HELP)
@click.argument("agent_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@click.option("--channel", "channel_name", type=click.Choice(OPENCLAW_CHANNELS, case_sensitive=False), required=True, help="目标 channel")
@click.option("--open-qr", is_flag=True, help="仅微信：在本地浏览器额外打开二维码链接")
@click.option("--app-id", "wps_xiezuo_app_id", default=None, help="仅 WPS 协作：开放平台应用 ID")
@click.option("--app-secret", "wps_xiezuo_app_secret", default=None, help="仅 WPS 协作：开放平台应用密钥")
@click.option("--account-id", "wps_xiezuo_account_id", default="default", help="仅 WPS 协作：账号 ID；当前只支持 default")
@click.option("--agent-id", "wps_xiezuo_agent_id", default="main", help="仅 WPS 协作：消息路由到的 OpenClaw agentId")
@click.option("--dm-policy", "wps_xiezuo_dm_policy", type=click.Choice(("disabled", "open", "pairing", "allowlist")), default="pairing", help="仅 WPS 协作：私聊策略，默认 pairing")
@click.option("--group-policy", "wps_xiezuo_group_policy", type=click.Choice(("open", "allowlist")), default="open", help="仅 WPS 协作：群聊策略，默认 open")
@click.option("--base-url", "wps_xiezuo_base_url", default="https://openapi.wps.cn", help="仅 WPS 协作：WPS OpenAPI 基础地址")
def channel_connect(
    agent_ref: Optional[str],
    region: Optional[str],
    channel_name: str,
    open_qr: bool,
    wps_xiezuo_app_id: Optional[str],
    wps_xiezuo_app_secret: Optional[str],
    wps_xiezuo_account_id: str,
    wps_xiezuo_agent_id: str,
    wps_xiezuo_dm_policy: str,
    wps_xiezuo_group_policy: str,
    wps_xiezuo_base_url: str,
):
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
                    await _config_apply_and_wait_for_reload(
                        preflight_gateway,
                        resolved_region,
                        detail,
                        config=config,
                        base_hash=base_hash,
                        note="ksadk seed weixin channel config",
                    )
            finally:
                await preflight_gateway.close()

            gateway = _build_gateway_client(resolved_region, detail)
            fallback_reason: str | None = None
            qr_url = ""
            wait_result: dict[str, Any] = {}
            try:
                await gateway.connect()
                methods = set(getattr(gateway, "methods", []) or [])
                missing_methods = [
                    method
                    for method in ("web.login.start", "web.login.wait")
                    if method not in methods
                ]
                if missing_methods:
                    fallback_reason = f"missing gateway methods: {', '.join(missing_methods)}"
                else:
                    try:
                        start = await gateway.web_login_start(force=False, timeout_ms=30_000)
                    except OpenClawGatewayRequestError as exc:
                        if not _is_weixin_web_login_unavailable(exc):
                            raise
                        fallback_reason = str(exc)
                    else:
                        qr_url = str(start.get("qrDataUrl") or "").strip()
                        session_key = str(start.get("sessionKey") or "").strip()
                        if not qr_url:
                            raise OpenClawGatewayError("微信扫码登录未返回二维码 URL")

                        print_success("请使用微信扫码完成连接")
                        print_kv("二维码链接", qr_url, value_style="#58a6ff")
                        _render_terminal_qr(qr_url)
                        if open_qr:
                            webbrowser.open(qr_url)

                        try:
                            wait_result = await gateway.web_login_wait(
                                account_id=session_key or None,
                                timeout_ms=120_000,
                            )
                        except OpenClawGatewayRequestError as exc:
                            if not _is_weixin_web_login_unavailable(exc):
                                raise
                            fallback_reason = str(exc)
            finally:
                await gateway.close()

            if fallback_reason:
                payload = await _run_weixin_remote_cli_login(
                    resolved_region,
                    detail,
                    reason=fallback_reason,
                )
                _emit_data_payload("OpenClaw Channel Connect", payload, subtitle="weixin")
                return

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

        if normalized_channel == "wps-xiezuo":
            app_id = str(wps_xiezuo_app_id or "").strip()
            app_secret = str(wps_xiezuo_app_secret or "").strip()
            account_id = str(wps_xiezuo_account_id or WPS_XIEZUO_DEFAULT_ACCOUNT_ID).strip() or WPS_XIEZUO_DEFAULT_ACCOUNT_ID
            if not app_id:
                raise click.ClickException("连接 WPS 协作 channel 必须提供 --app-id")
            if not app_secret:
                raise click.ClickException("连接 WPS 协作 channel 必须提供 --app-secret")
            if account_id != WPS_XIEZUO_DEFAULT_ACCOUNT_ID:
                raise click.ClickException("WPS 协作插件使用扁平 channel 配置，`--account-id` 仅支持 default")
            apply_gateway = _build_gateway_client(resolved_region, detail)
            changed = False
            try:
                await apply_gateway.connect()
                fresh_cfg_snapshot = await apply_gateway.config_get()
                fresh_config, fresh_base_hash = _extract_config_state(fresh_cfg_snapshot)
                changed = _mutate_wps_xiezuo_connect_config(
                    fresh_config,
                    app_id=app_id,
                    app_secret=app_secret,
                    account_id=account_id,
                    agent_id=str(wps_xiezuo_agent_id or "main").strip() or "main",
                    dm_policy=wps_xiezuo_dm_policy,
                    group_policy=wps_xiezuo_group_policy,
                    base_url=str(wps_xiezuo_base_url or "https://openapi.wps.cn").strip() or "https://openapi.wps.cn",
                )
                if changed:
                    await _config_apply_and_wait_for_reload(
                        apply_gateway,
                        resolved_region,
                        detail,
                        config=fresh_config,
                        base_hash=fresh_base_hash,
                        note="ksadk configure wps-xiezuo channel",
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
                "app_id": app_id,
                "account_id": account_id,
                "status": _extract_channel_snapshot(snapshot, normalized_channel),
            }
            _emit_data_payload("OpenClaw Channel Connect", payload, subtitle="wps-xiezuo")
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
                await _config_apply_and_wait_for_reload(
                    apply_gateway,
                    resolved_region,
                    detail,
                    config=changed_config,
                    base_hash=base_hash,
                    note="ksadk configure feishu channel",
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
            elif normalized_channel == "wps-xiezuo":
                changed = _mutate_wps_xiezuo_enabled(
                    config,
                    enabled=enabled,
                    account_id=account_id,
                )
                resolved_account_id = _channel_default_account_id(normalized_channel)
            else:
                changed = _mutate_feishu_enabled(config, enabled=enabled, account_id=account_id)
                resolved_account_id = _channel_default_account_id(normalized_channel)

            if changed:
                await _config_apply_and_wait_for_reload(
                    gateway,
                    resolved_region,
                    detail,
                    config=config,
                    base_hash=base_hash,
                    note=f"ksadk {action} {normalized_channel} channel",
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
            elif item == "wps-xiezuo":
                checks.append(
                    {
                        "name": "wps_xiezuo_plugin_visible",
                        "ok": plugin_id in plugin_entries or selected_snapshot is not None,
                        "plugin_id": plugin_id,
                    }
                )
                checks.append(
                    _build_channel_doctor_availability_check(
                        name="wps_xiezuo_status_snapshot",
                        available=selected_snapshot is not None,
                        configured=configured,
                        connect_required_message="WPS 协作尚未完成配置，首次 connect 前不会出现在 channel snapshot 中",
                    )
                )
                dep_check = _check_wps_xiezuo_local_deps()
                checks.append(
                    {
                        "name": "wps_xiezuo_local_deps",
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
@click.option(
    "--memory-system",
    type=click.Choice(["openclaw_default", "mem0"], case_sensitive=False),
    default=None,
    help="记忆后端类型（默认不显式变更当前服务端配置）",
)
@click.option("--mem0-instance-id", default=None, help="mem0 实例 ID")
@click.option("--mem0-instance-name", default=None, help="mem0 实例名称（可选）")
@click.option("--mem0-region", default=None, help="mem0 实例区域（可选）")
@click.option(
    "--env",
    "extra_env",
    multiple=True,
    help="额外透传自定义环境变量，格式 KEY=VALUE，可重复传入",
)
@click.option("--storage-size-gi", type=int, default=20, show_default=True, help="PVC 容量（Gi）")
@click.option("--storage-mount-path", default=None, help="PVC 挂载目录（默认: /home/node/.openclaw）")
@click.option("--no-storage", is_flag=True, help="禁用默认 PVC 挂载")
@network_options
@dry_run_option("仅显示请求，不实际部署")
def deploy(
    name: Optional[str],
    region: str,
    security_profile: Optional[str],
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    memory_system: Optional[str],
    mem0_instance_id: Optional[str],
    mem0_instance_name: Optional[str],
    mem0_region: Optional[str],
    extra_env: tuple[str, ...],
    storage_size_gi: int,
    storage_mount_path: Optional[str],
    no_storage: bool,
    enable_public_access: Optional[bool],
    enable_vpc_access: bool,
    vpc_id: Optional[str],
    subnet_id: Optional[str],
    security_group_id: Optional[str],
    availability_zone: Optional[str],
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
        agentengine openclaw deploy --image ghcr.io/myns/openclaw:v2
        # 透传业务自定义环境变量
        agentengine openclaw deploy --env APP_MODE=prod --env API_BASE=https://example.com
    """
    dry_run = effective_dry_run(dry_run)
    _build_openclaw_memory_config(
        memory_system=memory_system,
        mem0_instance_id=mem0_instance_id,
        mem0_instance_name=mem0_instance_name,
        mem0_region=mem0_region,
    )
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
                memory_system=memory_system,
                mem0_instance_id=mem0_instance_id,
                mem0_instance_name=mem0_instance_name,
                mem0_region=mem0_region,
                extra_env=extra_env,
                storage_size_gi=storage_size_gi,
                storage_mount_path=storage_mount_path,
                no_storage=no_storage,
                **network_cli_kwargs(
                    enable_public_access=enable_public_access,
                    enable_vpc_access=enable_vpc_access,
                    vpc_id=vpc_id,
                    subnet_id=subnet_id,
                    security_group_id=security_group_id,
                    availability_zone=availability_zone,
                ),
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
    memory_system: Optional[str],
    mem0_instance_id: Optional[str],
    mem0_instance_name: Optional[str],
    mem0_region: Optional[str],
    extra_env: tuple[str, ...] = (),
    storage_size_gi: int = 20,
    storage_mount_path: Optional[str] = None,
    no_storage: bool = False,
    enable_public_access: Optional[bool] = None,
    enable_vpc_access: bool = False,
    vpc_id: Optional[str] = None,
    subnet_id: Optional[str] = None,
    security_group_id: Optional[str] = None,
    availability_zone: Optional[str] = None,
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
    state_name = None
    state_kind = str(state.get("type") or state.get("framework") or "").strip().lower()
    if state_kind == "openclaw":
        existing_agent_id = state.get("agent_id")
        state_name = str(state.get("name") or "").strip() or None

    if name:
        openclaw_name = name
    elif state_name:
        openclaw_name = state_name
    elif project_name := _load_openclaw_project_name(project_dir):
        openclaw_name = project_name
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
    custom_env_vars = _parse_extra_openclaw_env_pairs(extra_env)
    if custom_env_vars:
        env_vars.update(custom_env_vars)
    env_vars = _normalize_openclaw_gateway_auth_env(env_vars)
    if not str(env_vars.get("OPENCLAW_MODEL_CATALOG_JSON") or "").strip():
        catalog_api_base = (
            model_base_url
            or _resolve_env("OPENCLAW_MODEL_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "LLM_API_BASE", "MODEL_API_BASE")
        )
        catalog_api_key = (
            model_api_key
            or _resolve_env("OPENCLAW_MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "MODEL_API_KEY")
        )
        provider_catalog = await fetch_provider_model_catalog(
            api_base=catalog_api_base,
            api_key=catalog_api_key,
        )
        selected_catalog = _filter_openclaw_provider_catalog(env_vars, provider_catalog)
        if _apply_openclaw_provider_model_catalog(env_vars, selected_catalog):
            print_info(f"已从模型服务 /v1/models 同步 OpenClaw 模型元数据: {len(selected_catalog)} 个")
    memory_config = _build_openclaw_memory_config(
        memory_system=memory_system,
        mem0_instance_id=mem0_instance_id,
        mem0_instance_name=mem0_instance_name,
        mem0_region=mem0_region,
    )

    print_title("OpenClaw 云端部署", f"region: {region}")
    print_kv("名称", openclaw_name)
    print_kv("镜像", image_ref)
    print_kv("区域", region, value_style="#58a6ff")
    if security_profile:
        print_kv("安全预设", security_profile.lower())
    if memory_config:
        print_kv("记忆后端", str(memory_config.get("memory_system") or "").strip())
        if memory_config.get("mem0_instance_id"):
            print_kv("mem0 实例", str(memory_config["mem0_instance_id"]))

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
    if memory_config:
        request_data["memory_config"] = memory_config
    storage_config = build_storage_config(
        "openclaw",
        no_storage=no_storage,
        mount_path=storage_mount_path,
        size_gi=storage_size_gi,
    )
    if storage_config:
        request_data["storage"] = storage_config
    network_payload = build_network_payload(
        enable_public_access=enable_public_access,
        enable_vpc_access=enable_vpc_access,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        availability_zone=availability_zone,
    )
    if network_payload:
        request_data["network"] = network_payload

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
                if memory_config:
                    update_payload["memory_config"] = memory_config
                if image_credential:
                    update_payload["image_credential"] = image_credential
                if storage_config:
                    update_payload["storage"] = storage_config
                if network_payload:
                    update_payload["network"] = network_payload
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
                    if memory_config:
                        update_payload["memory_config"] = memory_config
                    if image_credential:
                        update_payload["image_credential"] = image_credential
                    if storage_config:
                        update_payload["storage"] = storage_config
                    if network_payload:
                        update_payload["network"] = network_payload
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
                    latest = await get_latest_agent_access(
                        client,
                        agent_name=openclaw_name,
                        attempts=12,
                        interval_seconds=5,
                        include_api_key=True,
                        detail_fetcher=lambda agent_ref, include_api_key: _get_openclaw_detail_with_client(
                            client,
                            agent_ref,
                            include_api_key=include_api_key,
                        ),
                        suppress_transient_not_found_log=True,
                    )
                    if latest:
                        agent_id = latest.get("agent_id") or agent_id
                        endpoint = latest.get("endpoint") or endpoint
                        api_key = latest.get("api_key") or api_key
                        print_success(f"实例已创建: {agent_id}")
                    else:
                        print_warn("实例创建中，稍后使用 'agentengine openclaw list' 查看")
                elif agent_id and (
                    not str(endpoint or "").strip()
                    or not str(api_key or "").strip()
                ):
                    latest = await get_latest_agent_access(
                        client,
                        agent_id=str(agent_id).strip() or None,
                        attempts=5,
                        interval_seconds=1,
                        initial_delay_seconds=2,
                        require_complete_access=True,
                        include_api_key=True,
                        detail_fetcher=lambda agent_ref, include_api_key: _get_openclaw_detail_with_client(
                            client,
                            agent_ref,
                            include_api_key=include_api_key,
                        ),
                        suppress_transient_not_found_log=True,
                    )
                    if latest:
                        agent_id = latest.get("agent_id") or agent_id
                        endpoint = latest.get("endpoint") or endpoint
                        api_key = latest.get("api_key") or api_key

            # 保存状态
            saved_name = openclaw_name if not state.get("name") else state.get("name")
            if not existing_agent_id:
                saved_name = openclaw_name

            state_payload = {
                "type": "openclaw",
                "framework": "openclaw",
                "agent_id": agent_id,
                "name": saved_name,
                "region": region,
                "endpoint": endpoint,
                "api_key": api_key,
                "image": image_ref,
                "openclaw_auth_mode": env_vars.get("OPENCLAW_GATEWAY_AUTH_MODE"),
            }
            state_gateway_token = _openclaw_state_gateway_token(env_vars)
            if state_gateway_token:
                state_payload["openclaw_gateway_token"] = state_gateway_token
            save_state(project_dir, state_payload)

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
            resolved_agent_id = str(detail.get("agent_id") or agent_ref)
            render_descriptor_status(
                OPENCLAW_RESOURCE,
                subtitle=str(detail.get("name") or agent_ref),
                fields=[
                    ("ID", str(detail.get("agent_id") or "-"), None),
                    ("状态", str(status_val), status_rich_style(status_val)),
                    ("框架", str(detail.get("framework") or "-"), None),
                    ("区域", str(detail.get("region") or region), None),
                    ("Endpoint", str(detail.get("endpoint") or "N/A"), "#58a6ff"),
                    ("Langfuse", str(detail.get("langfuse_url") or "-"), "#58a6ff" if detail.get("langfuse_url") else None),
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
                    "langfuse_url": str(detail.get("langfuse_url") or ""),
                    "image": str(detail.get("artifact_path") or "-"),
                    "created_at": str(detail.get("created_at") or "-"),
                    "updated_at": str(detail.get("updated_at") or "-"),
                },
                next_steps=(
                    f"agentengine invoke {resolved_agent_id}",
                    f"agentengine openclaw tui {resolved_agent_id}",
                    f"agentengine dashboard open {resolved_agent_id} --path /chat",
                    "agentengine openclaw list",
                ),
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
        deleted_text = ", ".join(result["deleted"]) or "-"
        failed_text = ", ".join(result["failed"]) or "-"
        render_descriptor_status(
            OPENCLAW_RESOURCE,
            title="OpenClaw 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", deleted_text, "ok" if result["deleted"] else "muted"),
                ("失败", failed_text, "err" if result["failed"] else "muted"),
            ],
            next_steps=(
                "agentengine openclaw list",
                "agentengine openclaw deploy",
            ),
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
