"""KOP (Kingsoft Open Protocol) client — 公共 AICP 控制面调用层。

skill / a2a / mcp 等运行时模块通过本层访问 agentengine-server 对外 Action
（KOP 协议：`{base}/?Action=xxx&Version=xxx` + AWS V4 签名），避免每个模块
各自实现一遍鉴权 + URL 构造 + header 装配。

本层是同步（基于 requests + requests-aws4auth）；async 调用方用
``asyncio.to_thread`` 包裹。AICP 控制面是低频调用，同步足够。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import requests

from ksadk.common.auth import AWSV4Auth

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2024-06-12"
DEFAULT_REGION = "cn-beijing-6"
DEFAULT_SERVICE = "aicp"

# 精确匹配 AICP 控制面域名（公网 + 内网 + internal），不接受用户自定义 URL 走签名。
_AICP_HOSTS = {
    "aicp.api.ksyun.com",
    "aicp.inner.api.ksyun.com",
    "aicp.internal.api.ksyun.com",
}


def is_kop_endpoint(base_url: str) -> bool:
    """Return True only for exact AICP control-plane hosts.

    User-controlled URLs must never receive KOP signing headers.
    """
    host = (urlsplit(base_url or "").hostname or "").lower()
    return host in _AICP_HOSTS


class KOPError(RuntimeError):
    """KOP action 失败（非 2xx 或 Code != 0/200）。"""

    def __init__(self, *, code: int, message: str, request_id: str = "", action: str = "") -> None:
        super().__init__(f"KOP {action} failed: code={code} message={message}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.action = action


class KOPClient:
    """AICP KOP 控制面调用客户端。

    凭证优先级：构造参数 > KSADK_*_ACCESS_KEY/SECRET_KEY > KSYUN_ACCESS_KEY/SECRET_KEY。
    base_url 为空时按 region 探测默认 AICP（inner 优先,回落 public）。
    非 AICP base_url（如本地 server 直连）走普通 POST，不签名。
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
        service: Optional[str] = None,
        api_version: Optional[str] = None,
        account_id: Optional[str] = None,
        service_token: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("KSADK_A2A_SERVICE_URL") or "").strip().rstrip("/")
        if not self.base_url:
            inner = os.getenv("KSADK_A2A_SERVICE_ENDPOINT", "").strip()
            if inner:
                scheme = os.getenv("KSADK_A2A_SERVICE_SCHEME", "https").strip() or "https"
                self.base_url = f"{scheme}://{inner}".rstrip("/")
        if not self.base_url:
            # 默认探测：内网 inner 优先（runtime 在集群内），回落公网 public。
            self.base_url = self._detect_default_base_url()
        self.region = (
            region
            or os.getenv("KSADK_A2A_SERVICE_REGION")
            or os.getenv("KSYUN_REGION")
            or DEFAULT_REGION
        ).strip()
        self.service = (service or DEFAULT_SERVICE).strip()
        self.api_version = (api_version or DEFAULT_API_VERSION).strip()
        self.account_id = (
            account_id
            or os.getenv("KSADK_A2A_ACCOUNT_ID")
            or os.getenv("KSYUN_ACCOUNT_ID")
            or ""
        ).strip()
        self.service_token = (
            service_token or os.getenv("KSADK_A2A_SERVICE_TOKEN") or ""
        ).strip()
        self.timeout = timeout
        self._kop_mode = is_kop_endpoint(self.base_url)
        ak = (
            access_key
            or os.getenv("KSADK_A2A_ACCESS_KEY")
            or os.getenv("KSYUN_ACCESS_KEY")
            or ""
        ).strip()
        sk = (
            secret_key
            or os.getenv("KSADK_A2A_SECRET_KEY")
            or os.getenv("KSYUN_SECRET_KEY")
            or ""
        ).strip()
        self._auth = AWSV4Auth(
            access_key_id=ak,
            secret_access_key=sk,
            region=self.region,
            service=self.service,
        )
        self._session: Optional[requests.Session] = None

    @property
    def is_kop_mode(self) -> bool:
        return self._kop_mode

    def _session_obj(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _headers(self, action: str) -> dict[str, str]:
        import uuid

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.account_id:
            headers["X-Ksc-Account-Id"] = self.account_id
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        if self._kop_mode:
            headers["Host"] = urlsplit(self.base_url).netloc
            headers["X-Ksc-Request-Id"] = str(uuid.uuid4())
            headers["X-Ksc-Region"] = self.region
            headers["X-Ksc-Source"] = "ksadk-runtime"
            # 预发环境路由：AICP 据此把请求转发到预发 server（线上 KOP 默认路由到线上 server）。
            logical_region = (os.getenv("KSYUN_REGION") or "").strip().lower()
            if logical_region == "pre-online":
                headers["X-KSC-CUSTOM-SOURCE"] = os.getenv("AGENTENGINE_PRE_CUSTOM_SOURCE", "pre")
            if action:
                headers["X-Action"] = action
                headers["X-Version"] = self.api_version
        return headers

    def _build_url(self, action: str) -> str:
        if self._kop_mode:
            return f"{self.base_url}/?Action={action}&Version={self.api_version}"
        return f"{self.base_url}/agentengine/api/v1/{action}"

    def post_action(
        self, action: str, payload: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """POST 一个 KOP Action，返回 Data 字段（已解包信封）。

        非KOP 模式走 ``/agentengine/api/v1/{action}``；KOP 模式走 ``?Action=&Version=`` + 签名。
        """
        url = self._build_url(action)
        headers = self._headers(action)
        body = json.dumps(dict(payload or {}), ensure_ascii=False)
        auth = (
            self._auth.get_auth()
            if self._kop_mode and self._auth.is_enabled and not self.service_token
            else None
        )
        try:
            response = self._session_obj().post(
                url,
                data=body.encode("utf-8"),
                headers=headers,
                auth=auth,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise KOPError(
                code=503,
                message=f"KOP control plane unavailable: {exc}",
                action=action,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise KOPError(
                code=response.status_code,
                message=f"KOP HTTP error: {response.text[:200]}",
                action=action,
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise KOPError(
                code=response.status_code or 502,
                message="non-JSON response",
                action=action,
            ) from exc
        if not isinstance(envelope, dict):
            raise KOPError(
                code=response.status_code or 502,
                message="response not an object",
                action=action,
            )
        code = int(envelope.get("Code") or 0)
        if code and code != 200:
            raise KOPError(
                code=code,
                message=str(envelope.get("Message") or "KOP error"),
                request_id=str(envelope.get("RequestId") or ""),
                action=action,
            )
        data = envelope.get("Data")
        return data if isinstance(data, dict) else {}

    def _detect_default_base_url(self) -> str:
        """默认 AICP 地址探测：内网 inner 优先，失败回落公网 public。"""
        inner = "http://aicp.inner.api.ksyun.com"
        public = "https://aicp.api.ksyun.com"
        try:
            import socket
            host = urlsplit(inner).hostname or ""
            socket.create_connection((host, 80), timeout=1).close()
            return inner
        except OSError:
            return public
