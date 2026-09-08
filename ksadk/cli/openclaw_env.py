"""OpenClaw deploy 的运行时环境变量构建。

从 cmd_openclaw.py 拆出（模块体积治理）；``_resolve_env`` 与全局 env 缓存仍留在
cmd_openclaw（测试 monkeypatch 点），此处通过延迟 import 访问。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ksadk.deployment.env_forward import forward_shell_process_env
from ksadk.model_policy import build_runtime_model_policy_env

DEFAULT_TRUSTED_PROXY_USER_HEADER = "x-forwarded-user"
DEFAULT_TRUSTED_PROXY_CIDRS = [
    "127.0.0.1",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "35.0.0.0/8",
]


def _resolve_env(*keys: str, default: Optional[str] = None) -> Optional[str]:
    from ksadk.cli import cmd_openclaw

    return cmd_openclaw._resolve_env(*keys, default=default)


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


def _normalize_ui_locale(raw: Optional[str]) -> str:
    """标准化 UI 语言代码，默认 zh-CN。"""
    text = str(raw or "").strip()
    if not text:
        return "zh-CN"

    base = text.split(".", 1)[0].replace("_", "-").strip()
    low = base.lower()

    if low in {"c", "c-utf-8", "c.utf-8", "posix"}:
        return "zh-CN"
    if (
        low.startswith("zh-tw")
        or low.startswith("zh-hk")
        or low.startswith("zh-mo")
        or low.startswith("zh-hant")
    ):
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
            raise ValueError(
                "OPENCLAW_GATEWAY_TOKEN 与 OPENCLAW_GATEWAY_PASSWORD 同时提供时必须一致"
            )
        shared_secret = raw_token or raw_password
        if not shared_secret:
            raise ValueError(
                "OPENCLAW_GATEWAY_AUTH_MODE=token 时必须提供 "
                "OPENCLAW_GATEWAY_TOKEN 或 OPENCLAW_GATEWAY_PASSWORD"
            )
        normalized_env["OPENCLAW_GATEWAY_AUTH_MODE"] = "token"
        normalized_env["OPENCLAW_GATEWAY_TOKEN"] = shared_secret
        normalized_env["OPENCLAW_GATEWAY_PASSWORD"] = shared_secret
        return normalized_env

    if raw_token or raw_password:
        raise ValueError(
            "仅在 OPENCLAW_GATEWAY_AUTH_MODE=token 时支持 "
            "OPENCLAW_GATEWAY_TOKEN 或 OPENCLAW_GATEWAY_PASSWORD"
        )

    normalized_env["OPENCLAW_GATEWAY_AUTH_MODE"] = auth_mode
    normalized_env.pop("OPENCLAW_GATEWAY_TOKEN", None)
    normalized_env.pop("OPENCLAW_GATEWAY_PASSWORD", None)
    return normalized_env


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
    default_model_base_url = "https://kspmas.ksyun.com/v1"
    exec_profile_overrides = _resolve_exec_profile_overrides(security_profile)

    # 模型配置：客户端只透传用户显式配置和可选的 API Key；
    # 其余默认值交给镜像 bootstrap 兜底，避免创建请求把服务端默认行为短路掉。
    openclaw_explicit_model = default_model or _resolve_env("OPENCLAW_DEFAULT_MODEL")
    generic_model_preference = _resolve_env("OPENAI_MODEL_NAME", "MODEL_NAME", "LLM_MODEL")
    model_preference = openclaw_explicit_model or generic_model_preference
    explicit_base_url = model_base_url or _resolve_env(
        "OPENCLAW_MODEL_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"
    )
    base_url = _resolve_model_base_url(explicit_base_url)
    api_key = model_api_key or _resolve_env(
        "OPENCLAW_MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "MODEL_API_KEY"
    )
    model = model_preference or "glm-5.2"
    explicit_provider_id = model_provider_id or _resolve_env("OPENCLAW_MODEL_PROVIDER_ID")
    inferred_provider_id = explicit_provider_id
    if not inferred_provider_id and model and "/" in model:
        inferred_provider_id = model.split("/", 1)[0].strip()
    provider_id = inferred_provider_id or default_provider_id
    resolved_gateway_port = gateway_port or _resolve_env("OPENCLAW_GATEWAY_PORT", "PORT") or "8080"
    resolved_public_port = public_port or _resolve_env("OPENCLAW_PUBLIC_PORT") or "80"
    explicit_model_api = _resolve_env("OPENCLAW_MODEL_API")
    model_api = explicit_model_api or default_model_api
    trusted_proxy_user_header = (
        (
            _resolve_env(
                "OPENCLAW_TRUSTED_PROXY_USER_HEADER",
                "OPENCLAW_GATEWAY_TRUSTED_PROXY_USER_HEADER",
            )
            or DEFAULT_TRUSTED_PROXY_USER_HEADER
        )
        .strip()
        .lower()
    )
    internal_trusted_proxy_user = (
        _resolve_env("OPENCLAW_INTERNAL_TRUSTED_PROXY_USER") or "openclaw-backend"
    )
    internal_trusted_proxy_user_header = (
        (
            _resolve_env("OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER")
            or trusted_proxy_user_header
            or DEFAULT_TRUSTED_PROXY_USER_HEADER
        )
        .strip()
        .lower()
    )
    trusted_proxies = _normalize_csv_list(
        _resolve_env("OPENCLAW_TRUSTED_PROXIES") or "",
        default_items=DEFAULT_TRUSTED_PROXY_CIDRS,
    )
    browser_enabled = _resolve_env("OPENCLAW_BROWSER_ENABLED")
    browser_no_sandbox = _resolve_env("OPENCLAW_BROWSER_NO_SANDBOX") or "true"
    browser_headless = _resolve_env("OPENCLAW_BROWSER_HEADLESS") or "true"
    browser_executable = _resolve_env(
        "OPENCLAW_BROWSER_EXECUTABLE_PATH", "OPENCLAW_BROWSER_EXECUTABLE"
    )
    ui_locale = _normalize_ui_locale(_resolve_env("OPENCLAW_UI_LOCALE", "LANG", "LC_ALL"))
    exec_strict_mode_raw = (
        exec_profile_overrides.get("OPENCLAW_EXEC_STRICT_MODE")
        or _resolve_env("OPENCLAW_EXEC_STRICT_MODE", "OPENCLAW_EXEC_SAFE_MODE")
        or "false"
    )
    exec_strict_mode = _is_truthy(exec_strict_mode_raw)

    exec_host = (
        exec_profile_overrides.get("OPENCLAW_EXEC_HOST")
        or _resolve_env("OPENCLAW_EXEC_HOST")
        or "gateway"
    )
    exec_security = (
        exec_profile_overrides.get("OPENCLAW_EXEC_SECURITY")
        or _resolve_env("OPENCLAW_EXEC_SECURITY")
        or ("allowlist" if exec_strict_mode else "full")
    )
    exec_ask = (
        exec_profile_overrides.get("OPENCLAW_EXEC_ASK")
        or _resolve_env("OPENCLAW_EXEC_ASK")
        or "off"
    )
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
        or ("true" if exec_strict_mode else "false")
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
    env["OPENCLAW_TRUSTED_PROXY_USER_HEADER"] = (
        trusted_proxy_user_header or DEFAULT_TRUSTED_PROXY_USER_HEADER
    )
    env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER"] = internal_trusted_proxy_user
    env["OPENCLAW_INTERNAL_TRUSTED_PROXY_USER_HEADER"] = (
        internal_trusted_proxy_user_header
        or trusted_proxy_user_header
        or DEFAULT_TRUSTED_PROXY_USER_HEADER
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
            resolved_model = (
                f"{provider_id}/{normalized_model}" if provider_id else normalized_model
            )
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

    env = _normalize_openclaw_gateway_auth_env(env)
    env = build_runtime_model_policy_env(env, runtime="openclaw")
    # shell 前缀转发 (KSADK_/OPENAI_/KSYUN_/E2B_ + allowlist)，对齐通用 deploy；
    # setdefault 语义不覆盖上面已 resolve 的固定键，--env/--env-file 仍可覆盖。
    forward_shell_process_env(env)
    return env
