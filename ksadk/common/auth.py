"""
AWS Signature V4 签名认证

基于 requests-aws4auth 库封装，用于金山云服务间调用签名认证。

使用场景：
- 调用 Serverless API (AgentRuntime CRUD)
- 调用 KCR API (镜像仓库)
- 调用其他需要签名的云服务

使用方式：
    from ksadk.common import AWSV4Auth

    auth = AWSV4Auth(
        access_key_id="your-ak",
        secret_access_key="your-sk",
        region="cn-beijing-6",
        service="aicp",
    )

    # 用于 requests
    response = requests.post(url, auth=auth.get_auth(), headers=headers)
"""

import os
import logging
from typing import Dict, Optional

from requests_aws4auth import AWS4Auth

logger = logging.getLogger(__name__)


class AWSV4Auth:
    """AWS V4 签名认证器

    基于 requests-aws4auth 封装，支持 requests 库。

    Attributes:
        access_key_id: 访问密钥 ID (AK)
        secret_access_key: 访问密钥 (SK)
        region: 区域 ID
        service: 服务名称
    """

    def __init__(
        self,
        access_key_id: str = "",
        secret_access_key: str = "",
        region: str = "cn-beijing-6",
        service: str = "aicp",
    ):
        """初始化签名器

        Args:
            access_key_id: 访问密钥 ID (AK)，默认从环境变量读取
            secret_access_key: 访问密钥 (SK)，默认从环境变量读取
            region: 区域 ID (如 cn-beijing-6)
            service: 服务名称 (如 kmr, aicp, iam 等)
        """
        self.access_key_id = (
            access_key_id
            or os.environ.get("KSYUN_ACCESS_KEY")
            or os.environ.get("KS3_ACCESS_KEY", "")
        )
        self.secret_access_key = (
            secret_access_key
            or os.environ.get("KSYUN_SECRET_KEY")
            or os.environ.get("KS3_SECRET_KEY", "")
        )
        self.region = region
        self.service = service

        self._auth: Optional[AWS4Auth] = None

        if self.access_key_id and self.secret_access_key:
            self._auth = AWS4Auth(
                self.access_key_id,
                self.secret_access_key,
                self.region,
                self.service,
            )
            logger.debug(f"AWSV4Auth initialized: service={service}, region={region}")
        else:
            logger.debug("AWSV4Auth: credentials not provided, signing disabled")

    @property
    def is_enabled(self) -> bool:
        """是否启用签名"""
        return self._auth is not None

    def get_auth(self) -> Optional[AWS4Auth]:
        """获取 requests-aws4auth 的 Auth 对象

        用于 requests 库:
            response = requests.post(url, auth=auth.get_auth(), headers=headers)

        Returns:
            AWS4Auth 对象，如果未配置凭证则返回 None
        """
        return self._auth

    def sign_request(self, request) -> None:
        """对 requests.PreparedRequest 进行签名

        Args:
            request: requests.PreparedRequest 对象
        """
        if self._auth:
            self._auth(request)

    def sign_headers(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: str = "",
    ) -> Dict[str, str]:
        """手动签名并返回带签名的 headers

        用于需要手动处理 headers 的场景。

        Args:
            method: HTTP 方法 (GET, POST 等)
            url: 完整 URL
            headers: 原始请求头
            body: 请求体

        Returns:
            带签名的请求头字典
        """
        if not self._auth:
            return headers

        import requests

        # 创建一个 PreparedRequest 来获取签名
        req = requests.Request(
            method=method,
            url=url,
            headers=headers,
            data=body.encode("utf-8") if body else None,
        )
        prepared = req.prepare()

        # 签名
        self._auth(prepared)

        # 返回签名后的 headers
        return dict(prepared.headers)


def create_auth(
    access_key_id: str = "",
    secret_access_key: str = "",
    region: str = "cn-beijing-6",
    service: str = "aicp",
) -> AWSV4Auth:
    """创建签名认证器的便捷函数

    如果未提供凭证，将从环境变量读取:
    - KSYUN_ACCESS_KEY / KS3_ACCESS_KEY
    - KSYUN_SECRET_KEY / KS3_SECRET_KEY

    Args:
        access_key_id: AK
        secret_access_key: SK
        region: 区域
        service: 服务名称

    Returns:
        AWSV4Auth 实例

    Example:
        # 使用环境变量
        auth = create_auth(region="cn-beijing-6", service="aicp")

        # 指定凭证
        auth = create_auth(
            access_key_id="your-ak",
            secret_access_key="your-sk",
            service="kcr",
        )
    """
    return AWSV4Auth(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=region,
        service=service,
    )
