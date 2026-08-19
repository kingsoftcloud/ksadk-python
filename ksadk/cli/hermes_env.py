"""Hermes deploy 的运行时环境变量构建。

从 cmd_hermes.py 拆出（模块体积治理）；``_env_value`` 与全局 env 缓存仍留在
cmd_hermes（测试 monkeypatch 点），此处通过延迟 import 访问。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ksadk.cli.env_options import apply_explicit_env_with_shell_priority
from ksadk.configs.env_registry import is_sensitive_env_var
from ksadk.deployment.env_forward import forward_shell_process_env
from ksadk.model_policy import build_runtime_model_policy_env


def _env_value(*names: str) -> str:
    from ksadk.cli import cmd_hermes

    return cmd_hermes._env_value(*names)


def _normalize_hermes_ui_locale(raw: Optional[str]) -> str:
    """标准化 Hermes UI 语言代码，当前 upstream 只支持 en / zh。"""
    text = str(raw or "").strip()
    if not text:
        return "zh"

    base = text.split(".", 1)[0].replace("_", "-").strip().lower()
    if base in {"c", "c-utf-8", "c.utf-8", "posix"}:
        return "zh"
    if base.startswith("en"):
        return "en"
    if base.startswith("zh"):
        return "zh"
    return "zh"


def _default_context_length_for_model(model: str | None) -> str:
    from ksadk.cli import cmd_hermes

    normalized = str(model or "").strip().lower()
    if not normalized:
        return ""
    for model_fragment, context_length in cmd_hermes.DEFAULT_HERMES_CONTEXT_LENGTHS:
        if model_fragment in normalized:
            return context_length
    return ""


def _normalize_hermes_runtime_base_url(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    return normalized


def _build_hermes_env_vars(
    *,
    model_base_url: str | None = None,
    model_api_key: str | None = None,
    default_model: str | None = None,
    model_metadata: dict[str, Any] | None = None,
    cli_env: dict[str, str] | None = None,
    auto_dotenv: dict[str, str] | None = None,
    shell_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    from ksadk.cli import cmd_hermes

    raw_model_base_url = model_base_url or _env_value("OPENAI_BASE_URL")
    resolved_model_base_url = (
        _normalize_hermes_runtime_base_url(raw_model_base_url)
        if raw_model_base_url
        else cmd_hermes.DEFAULT_HERMES_RUNTIME_BASE_URL
    )
    resolved_default_model = (
        default_model or _env_value("OPENAI_MODEL_NAME") or cmd_hermes.DEFAULT_HERMES_MODEL_NAME
    )
    metadata_context_length = ""
    if isinstance(model_metadata, dict):
        metadata_context_length = str(model_metadata.get("context_window_tokens") or "").strip()
    context_length = (
        _env_value("HERMES_CONTEXT_LENGTH", "OPENAI_CONTEXT_LENGTH", "MODEL_CONTEXT_LENGTH")
        or metadata_context_length
        or _default_context_length_for_model(resolved_default_model)
    )
    ui_locale = _normalize_hermes_ui_locale(_env_value("HERMES_UI_LOCALE", "LANG", "LC_ALL"))
    raw = {
        "OPENAI_API_KEY": model_api_key or _env_value("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": resolved_model_base_url,
        "OPENAI_MODEL_NAME": resolved_default_model,
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
        "HERMES_DASHBOARD_HOST": "127.0.0.1",
        "HERMES_DASHBOARD_PORT": "9119",
        "KSADK_RUNTIME_PORT": _env_value("PORT") or "8080",
        "HERMES_UI_LOCALE": ui_locale,
    }
    if context_length:
        raw["HERMES_CONTEXT_LENGTH"] = context_length
    fallback_model = _env_value("HERMES_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL_NAME")
    if fallback_model:
        raw["HERMES_FALLBACK_PROVIDER"] = _env_value("HERMES_FALLBACK_PROVIDER") or "custom"
        raw["HERMES_FALLBACK_MODEL"] = fallback_model
        raw["HERMES_FALLBACK_BASE_URL"] = (
            _env_value("HERMES_FALLBACK_BASE_URL") or resolved_model_base_url
        )
    api_server_key = _env_value("API_SERVER_KEY", "HERMES_API_SERVER_KEY")
    if api_server_key:
        raw["API_SERVER_KEY"] = api_server_key
    # Observability routes and credentials are platform-managed. The Hermes
    # deploy CLI must not translate or forward legacy Langfuse SDK variables;
    # server/runtime inject the standard OTLP primary and CloudMonitor secondary.
    for key in (
        "WPSXIEZUO_APP_ID",
        "WPSXIEZUO_APP_KEY",
        "WPSXIEZUO_API_BASE",
        "WPSXIEZUO_WS_ENDPOINT",
        "WPSXIEZUO_GROUP_AT_ONLY",
        "WPSXIEZUO_ALLOWED_USERS",
        "WPSXIEZUO_ALLOW_ALL_USERS",
        "WPSXIEZUO_HOME_CHANNEL",
    ):
        value = _env_value(key)
        if value:
            raw[key] = value
    raw = build_runtime_model_policy_env(raw, runtime="hermes")
    # shell 前缀转发 (KSADK_/OPENAI_/KSYUN_/E2B_ + allowlist)，对齐通用 deploy；
    # setdefault 语义不覆盖上面已 resolve 的固定键，--env/--env-file 仍可覆盖。
    forward_shell_process_env(raw)
    if raw.get("HERMES_FALLBACK_MODEL"):
        raw.setdefault(
            "HERMES_FALLBACK_PROVIDER", _env_value("HERMES_FALLBACK_PROVIDER") or "custom"
        )
        raw.setdefault(
            "HERMES_FALLBACK_BASE_URL",
            _env_value("HERMES_FALLBACK_BASE_URL") or resolved_model_base_url,
        )
    if cli_env or auto_dotenv:
        apply_explicit_env_with_shell_priority(
            raw, cli_env or {}, auto_dotenv or {}, shell_keys or set(os.environ)
        )
    return [
        {
            "Key": key,
            "Value": str(value),
            "IsSensitive": is_sensitive_env_var(key),
        }
        for key, value in raw.items()
        if value is not None and str(value).strip() != ""
    ]
