"""
agentengine openclaw - OpenClaw 云端部署管理

设计目标:
- 和 Agent 部署完全一致，复用 CreateAgentProduct 接口 (Container 模式)
- Framework 标记为 "openclaw"，区分于普通 Agent
- 预构建公共镜像，用户无需自行构建
- 模型配置通过 EnvironmentVariables 传递，自动复用 OPENAI_* 变量
"""

from __future__ import annotations

import os
import asyncio
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import click
from rich.measure import Measurement
from rich.table import Table as RichTable

from ksadk.api.client import DryRunExit
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import print_exception
from ksadk.cli.ui import (
    get_console,
    new_table,
    print_error,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
    status_rich_style,
)

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


@click.group("openclaw")
def openclaw():
    """OpenClaw 云端部署管理

    \b
    示例:
        # 1) 部署 OpenClaw 到云端
        agentengine openclaw deploy
        # 2) 查看已部署的 OpenClaw
        agentengine openclaw list
        # 3) 查看状态
        agentengine openclaw status <id>
        # 4) 删除
        agentengine openclaw delete <id>
    """
    pass


@openclaw.command("deploy")
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
        import json
        print_rule("Dry Run — 请求数据")
        console.print(json.dumps(request_data, indent=2, ensure_ascii=False))
        return

    # 调用 API
    print_rule("部署 OpenClaw")
    try:
        latest_status = None
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

            # 再读一次状态，避免把“已创建”误认为“已稳定运行”。
            if agent_id:
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
    except Exception as e:
        print_exception("部署失败", e)


@openclaw.command("list")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@dry_run_option()
def list_openclaws(region: str, dry_run: bool):
    """列出已部署的 OpenClaw 实例"""
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    async def _list():
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                resp = await client.list_agents(region=region, framework="openclaw", page_size=100)
                agents = resp.get("agents", []) or []
                if not agents:
                    print_warn("没有找到已部署的 OpenClaw 实例")
                    return

                total = int(resp.get("total") or len(agents))
                account_summary = _summarize_openclaw_account(agents)
                region_summary = _summarize_openclaw_region(agents, region)
                table = new_table("已部署 OpenClaw")
                table.add_column("ID", style="#58a6ff", no_wrap=True)
                table.add_column("NAME", style="white")
                table.add_column("STATUS", no_wrap=True, justify="center")
                table.add_column("ENDPOINT", style="#8b949e", overflow="ellipsis")
                table.add_column("REGION", style="#8b949e")
                for a in agents:
                    status = (a.get("status") or "UNKNOWN").upper()
                    table.add_row(
                        a.get("agent_id", "-"),
                        a.get("name", "-"),
                        f"[{status_rich_style(status)}]{status}[/]",
                        a.get("endpoint", "N/A"),
                        a.get("region", "-"),
                    )
                console.print(table)
                _print_openclaw_list_summary(
                    table,
                    f"账号: {account_summary}  region: {region_summary}  总计: {total}",
                )

        except DryRunExit:
            raise
        except Exception as e:
            print_exception("获取列表失败", e)

    run_async_with_dry_run(_list(), dry_run=dry_run)


@openclaw.command("status")
@click.argument("agent_ref", required=False, default=None)
@click.option("--region", "-r", default=None, help="区域 (默认优先读取 .agentengine.state)")
@dry_run_option()
def status(agent_ref: Optional[str], region: Optional[str], dry_run: bool):
    """查看 OpenClaw 状态

    \b
    AGENT_REF: Agent ID 或名称 (可选，默认从 .agentengine.state 读取)
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.deployment.state import load_state

    state = load_state(Path(".").resolve())
    region = _resolve_region(region, state)

    # 无参数时从本地状态读取
    if not agent_ref:
        if state.get("type") == "openclaw" and state.get("agent_id"):
            agent_ref = state["agent_id"]
            print_info(f"从本地状态读取: {agent_ref}")
        elif state.get("type") == "openclaw" and state.get("name"):
            agent_ref = state["name"]
        else:
            print_error("请指定 Agent ID 或在部署目录下运行")
            return
    from ksadk.api import AgentEngineClient

    async def _get():
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                # 尝试按 ID 查询，失败则按 Name
                if agent_ref.startswith("ar-"):
                    agent = await client.get_agent(agent_id=agent_ref)
                else:
                    agent = await client.get_agent(name=agent_ref)

                if not agent:
                    print_error(f"未找到 OpenClaw: {agent_ref}")
                    return

                detail = _flatten_agent_detail(agent)
                print_title("OpenClaw 状态", detail.get("name") or str(agent_ref))
                print_kv("ID", detail.get("agent_id") or "-")

                status_val = detail.get("status", "UNKNOWN")
                print_kv("Status", status_val, value_style=status_rich_style(status_val))
                print_kv("Framework", detail.get("framework") or "-")
                print_kv("Region", detail.get("region") or region)
                print_kv("Endpoint", detail.get("endpoint") or "N/A", value_style="#58a6ff")
                print_kv("镜像", detail.get("artifact_path") or "-")
                print_kv("Created", str(detail.get("created_at") or "-"))
                print_kv("Updated", str(detail.get("updated_at") or "-"))

        except DryRunExit:
            raise
        except Exception as e:
            print_exception("获取状态失败", e)
            # 回退：显示本地状态，至少给出排障上下文
            if state and state.get("type") == "openclaw":
                print_rule("本地状态回退")
                print_kv("ID", str(state.get("agent_id", "-")))
                print_kv("Name", str(state.get("name", "-")))
                print_kv("Region", str(state.get("region", region)))
                print_kv("Endpoint", str(state.get("endpoint", "N/A")))
                print_kv("API Key", "已保存" if state.get("api_key") else "未保存")
                print_info("提示: 检查 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY 或 region 参数")

    run_async_with_dry_run(_get(), dry_run=dry_run)


@openclaw.command("delete")
@click.argument("agent_ref")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@dry_run_option()
def delete(agent_ref: str, region: str, yes: bool, dry_run: bool):
    """删除 OpenClaw 实例

    AGENT_REF: Agent ID
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    if not yes and not dry_run:
        if not click.confirm("确定要删除这个 OpenClaw 实例吗?"):
            print_info("已取消")
            return

    async def _delete():
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                success = await client.delete_agent(agent_ref)
                if success:
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
                    print_error("删除失败")

        except DryRunExit:
            raise
        except Exception as e:
            print_exception("删除失败", e)

    run_async_with_dry_run(_delete(), dry_run=dry_run)
