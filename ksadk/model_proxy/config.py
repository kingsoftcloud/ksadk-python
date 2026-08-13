"""ProxyConfig:转换 proxy 的配置(显式注入,不在模块导入时读 env,便于内化与测试)。"""

import os
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

# HTTPS(凭证安全):不再用明文 HTTP 携带 Bearer key。
DEFAULT_UPSTREAM_BASE = "https://kspmas.ksyun.com/v1"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class ProxyConfig:
    upstream_base: str = DEFAULT_UPSTREAM_BASE
    api_key: str = ""  # 上游凭证(proxy -> 上游)
    local_token: str = ""  # 本地 proxy 鉴权 token(codex -> proxy);空则不校验(仅本地调试)
    # 非空时 responses 转换路径强制改写 model（Codex 可能发送内部伪模型名）。
    upstream_model: str = ""
    timeout: float = 180.0
    event_callback: Callable[[str, dict[str, Any]], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self.upstream_base = _require_secure_upstream(self.upstream_base.rstrip("/"))

    def emit(self, event: str, data: dict[str, Any]) -> None:
        callback = self.event_callback
        if callback is None:
            return
        try:
            callback(event, data)
        except Exception:
            # Observability must never break model traffic.
            return

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            upstream_base=os.environ.get("UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE),
            api_key=os.environ.get("KSPMAS_API_KEY", ""),
            local_token=os.environ.get("KSADK_PROXY_TOKEN", ""),
            upstream_model=os.environ.get("OPENAI_MODEL_NAME")
            or os.environ.get("MODEL_NAME")
            or "",
            timeout=float(os.environ.get("UPSTREAM_TIMEOUT", "180")),
        )


def _require_secure_upstream(upstream: str) -> str:
    """凭证安全:scheme 仅 http/https;非回环主机必须 https(避免明文携带 Bearer)。

    用 urlsplit 规范化 scheme/hostname,堵 `HTTP://`(大小写)与 `ftp://` 绕过。
    """
    parts = urlsplit(upstream)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的上游 scheme(仅 http/https): {upstream}")
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ValueError(f"上游必须使用 https(携带凭证,禁明文 http): {upstream}")
    return upstream
