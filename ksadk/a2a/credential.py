"""A2ACredentialProvider — 按 credential binding 获取短时出站凭据 (goal-06 §3.2)。

契约 §3.2:``A2ACredentialProvider`` 按 credential binding handle 解析出站调用凭据;
``credential_scheme_name``/``credential_secret_ref`` 表达 external 出站凭据 scheme 与
Secret reference(可空)。7 月范围(§「7 月 credential adapter」):支持 **none / HTTP
bearer / HTTP basic / API key**;**OAuth2/OIDC 未支持**——注册可保存 schema,但调用返回
明确 capability error,不静默成功也不伪造 token。
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class CredentialCapabilityError(RuntimeError):
    """credential scheme 超出当前能力(OAuth2/OIDC 未支持)时抛出。"""


@dataclass
class OutboundCredential:
    """解析后的出站凭据(已物化为 HTTP 头;``scheme`` 记录来源 scheme)。"""

    scheme: str  # "none" | "bearer" | "basic" | "apikey"
    headers: dict[str, str] = field(default_factory=dict)


class A2ACredentialProvider(ABC):
    """按 credential binding handle 解析出站凭据。"""

    @abstractmethod
    async def resolve(self, credential_handle: Optional[str]) -> OutboundCredential:
        """把 credential binding handle 解析为出站 HTTP 头。无 handle → ``none``。"""
        raise NotImplementedError


#: 7 月支持的 scheme(其余注册可存,调用报 capability error)。
_SUPPORTED_SCHEMES = frozenset({"none", "bearer", "basic", "apikey"})
_UNSUPPORTED_OAUTH_SCHEMES = frozenset({"oauth2", "oidc", "oauth2_client_credentials"})


class StaticCredentialProvider(A2ACredentialProvider):
    """从注入的 ``handle -> binding`` 映射解析出站凭据(测试/本地;生产由 STS/Secret 后端替换)。

    binding 结构::

        {"scheme": "bearer", "token": "..."}
        {"scheme": "basic", "username": "...", "password": "..."}
        {"scheme": "apikey", "header": "X-API-Key", "key": "..."}
        {"scheme": "none"}
        {"scheme": "oauth2", ...}   # 注册可存,resolve 抛 CredentialCapabilityError
    """

    def __init__(self, bindings: Optional[dict[str, dict[str, Any]]] = None) -> None:
        self._bindings = dict(bindings or {})

    def register(self, handle: str, binding: dict[str, Any]) -> None:
        self._bindings[handle] = binding

    async def resolve(self, credential_handle: Optional[str]) -> OutboundCredential:
        if not credential_handle:
            return OutboundCredential(scheme="none", headers={})
        binding = self._bindings.get(credential_handle)
        if binding is None:
            raise KeyError(f"未知 credential binding handle: {credential_handle!r}")
        scheme = str(binding.get("scheme") or "none").lower()
        if scheme in _UNSUPPORTED_OAUTH_SCHEMES:
            raise CredentialCapabilityError(
                f"credential scheme {scheme!r} 当前未支持(7 月仅 none/bearer/basic/apikey);"
                " 注册可保存 schema,调用返回明确 capability error"
            )
        if scheme not in _SUPPORTED_SCHEMES:
            raise CredentialCapabilityError(f"未知 credential scheme: {scheme!r}")

        if scheme == "bearer":
            return OutboundCredential(
                scheme="bearer",
                headers={"Authorization": f"Bearer {binding.get('token', '')}"},
            )
        if scheme == "basic":
            raw = f"{binding.get('username', '')}:{binding.get('password', '')}"
            token = base64.b64encode(raw.encode()).decode()
            return OutboundCredential(scheme="basic", headers={"Authorization": f"Basic {token}"})
        if scheme == "apikey":
            header = str(binding.get("header") or "X-API-Key")
            return OutboundCredential(
                scheme="apikey", headers={header: str(binding.get("key", ""))}
            )
        return OutboundCredential(scheme="none", headers={})


__all__ = [
    "A2ACredentialProvider",
    "CredentialCapabilityError",
    "OutboundCredential",
    "StaticCredentialProvider",
]
