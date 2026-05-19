"""UI 配置解析与持久化辅助函数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit


UI_PROFILE_AUTO = "auto"
UI_PROFILE_ADK = "adk"
UI_PROFILE_LANGCHAIN = "langchain"
UI_PROFILE_OPENCLAW = "openclaw"
UI_PROFILE_HERMES = "hermes"
UI_PROFILE_CUSTOM = "custom"

SUPPORTED_UI_PROFILES = (
    UI_PROFILE_AUTO,
    UI_PROFILE_ADK,
    UI_PROFILE_LANGCHAIN,
    UI_PROFILE_OPENCLAW,
    UI_PROFILE_HERMES,
    UI_PROFILE_CUSTOM,
)


_FRAMEWORK_TO_PROFILE = {
    "adk": UI_PROFILE_ADK,
    "langchain": UI_PROFILE_LANGCHAIN,
    "langgraph": UI_PROFILE_LANGCHAIN,
    "deepagents": UI_PROFILE_LANGCHAIN,
    "openclaw": UI_PROFILE_OPENCLAW,
    "hermes": UI_PROFILE_HERMES,
}

_DEFAULT_PATH_BY_PROFILE = {
    UI_PROFILE_ADK: "/chat",
    UI_PROFILE_LANGCHAIN: "/chat",
    UI_PROFILE_OPENCLAW: "/chat",
    UI_PROFILE_HERMES: "/chat",
    UI_PROFILE_CUSTOM: "/",
}


@dataclass(frozen=True)
class UIConfig:
    """解析后的 UI 配置。"""

    profile: str
    path: str
    url: Optional[str] = None


def normalize_ui_profile(profile: Optional[str], *, default: str = UI_PROFILE_AUTO) -> str:
    value = (profile or "").strip().lower()
    if not value:
        return default
    if value not in SUPPORTED_UI_PROFILES:
        raise ValueError(f"unsupported ui_profile: {profile}")
    return value


def normalize_ui_path(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None

    value = path.strip()
    if not value:
        return None

    if not value.startswith("/"):
        value = "/" + value
    return value


def normalize_ui_url(url: Optional[str]) -> Optional[str]:
    if url is None:
        return None
    value = url.strip()
    return value or None


def infer_ui_profile_from_framework(framework: Optional[str]) -> str:
    key = (framework or "").strip().lower()
    return _FRAMEWORK_TO_PROFILE.get(key, UI_PROFILE_ADK)


def default_ui_path(profile: str) -> str:
    return _DEFAULT_PATH_BY_PROFILE.get(profile, "/")


def extract_ui_state(state: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not isinstance(state, dict):
        return None, None, None

    nested = state.get("ui") if isinstance(state.get("ui"), dict) else {}

    profile = state.get("ui_profile") or nested.get("profile")
    path = state.get("ui_path") or nested.get("path")
    url = state.get("ui_url") or nested.get("url")

    return profile, path, url


def resolve_ui_config(
    *,
    framework: Optional[str],
    state: Optional[Dict[str, Any]],
    cli_profile: Optional[str],
    cli_path: Optional[str],
    cli_url: Optional[str],
) -> UIConfig:
    """合并 CLI + state + framework 默认，得到最终 UI 配置。

    优先级:
    1) CLI 参数
    2) .agentengine.state
    3) framework 默认
    4) 全局默认
    """

    state_profile, state_path, state_url = extract_ui_state(state)

    profile = normalize_ui_profile(cli_profile or state_profile or UI_PROFILE_AUTO)
    if profile == UI_PROFILE_AUTO:
        profile = infer_ui_profile_from_framework(framework)

    path = normalize_ui_path(cli_path) if cli_path is not None else normalize_ui_path(state_path)
    url = normalize_ui_url(cli_url) if cli_url is not None else normalize_ui_url(state_url)

    # 兼容历史配置: 旧版本 runtime UI 默认路径曾经是 / 或 /langchain，
    # 现统一收口到 /chat，避免 dashboard/share 默认落到 runtime 根路径。
    if profile in {UI_PROFILE_ADK, UI_PROFILE_LANGCHAIN, UI_PROFILE_OPENCLAW, UI_PROFILE_HERMES}:
        normalized_legacy_path = (path or "").rstrip("/") or "/"
        legacy_paths = {"/langchain"}
        if cli_path is None:
            legacy_paths.add("/")
        if normalized_legacy_path in legacy_paths:
            path = "/chat"

    if not path:
        path = default_ui_path(profile)

    return UIConfig(profile=profile, path=path, url=url)


def ui_config_to_state_fields(config: UIConfig) -> Dict[str, Any]:
    return {
        "ui_profile": config.profile,
        "ui_path": config.path,
        "ui_url": config.url,
    }


def is_same_origin(url: str, endpoint: str) -> bool:
    if not url or not endpoint:
        return False

    left = urlsplit(url)
    right = urlsplit(endpoint)

    if not left.scheme or not left.netloc or not right.scheme or not right.netloc:
        return False

    return (left.scheme.lower(), left.netloc.lower()) == (
        right.scheme.lower(),
        right.netloc.lower(),
    )
