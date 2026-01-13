"""
Serverless API Client - 金山云 Serverless AgentRuntime API 客户端

直接调用 Serverless 内网接口，下一期会切换为调用服务端。

支持 AWS V4 签名认证，用于跨服务调用鉴权。
"""

import os
import uuid
import json
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum

import requests

from ksadk.common.auth import AWSV4Auth
from ksadk.common.constants import DEFAULT_SERVERLESS_ENDPOINT

logger = logging.getLogger(__name__)


# ============================================================================
# 常量定义
# ============================================================================

# API 路径 (内网 API 使用简化路径)
API_CREATE_AGENT_RUNTIME = "/CreateAgentRuntime"
API_GET_AGENT_RUNTIME = "/GetAgentRuntime"
API_DELETE_AGENT_RUNTIME = "/DeleteAgentRuntime"
API_LIST_AGENT_RUNTIMES = "/ListAgentRuntimes"


# ============================================================================
# 数据模型
# ============================================================================


class ArtifactType(str, Enum):
    """部署类型"""

    CODE = "Code"
    CONTAINER = "Container"


@dataclass
class CodeConfiguration:
    """Code 模式配置"""

    ks3_bucket_name: str
    ks3_object_name: str
    ks3_region: str
    ks3_access_key: str
    ks3_secret_key: str
    language: str = "python"
    command: List[str] = field(
        default_factory=lambda: [
            "python",
            "-m",
            "uvicorn",
            "entrypoint:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
        ]
    )
    target_path: str = "/app/code"
    archive_type: str = "zip"
    ks3_endpoint: Optional[str] = None
    checksum: Optional[str] = None
    downloader_image: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "ks3BucketName": self.ks3_bucket_name,
            "ks3ObjectName": self.ks3_object_name,
            "ks3Region": self.ks3_region,
            "ks3AccessKey": self.ks3_access_key,
            "ks3SecretKey": self.ks3_secret_key,
            "language": self.language,
            "command": self.command,
            "targetPath": self.target_path,
            "archiveType": self.archive_type,
        }
        if self.ks3_endpoint:
            result["ks3Endpoint"] = self.ks3_endpoint
        if self.checksum:
            result["checksum"] = self.checksum
        if self.downloader_image:
            result["downloaderImage"] = self.downloader_image
        return result


@dataclass
class ContainerConfiguration:
    """Container 模式配置"""

    image: str
    command: List[str] = field(default_factory=lambda: ["python", "entrypoint.py"])
    args: List[str] = field(default_factory=list)
    image_registry_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "image": self.image,
            "command": self.command,
        }
        if self.args:
            result["args"] = self.args
        if self.image_registry_type:
            result["imageRegistryType"] = self.image_registry_type
        return result


@dataclass
class HealthCheckConfiguration:
    """健康检查配置"""

    http_get_url: str = "/health"
    initial_delay_seconds: int = 10
    period_seconds: int = 10
    timeout_seconds: int = 5
    success_threshold: int = 1
    failure_threshold: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "httpGetUrl": self.http_get_url,
            "initialDelaySeconds": self.initial_delay_seconds,
            "periodSeconds": self.period_seconds,
            "timeoutSeconds": self.timeout_seconds,
            "successThreshold": self.success_threshold,
            "failureThreshold": self.failure_threshold,
        }


@dataclass
class ModelConfiguration:
    """LLM 模型配置"""

    model_name: str
    model_api_base: str
    model_api_key: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "modelName": self.model_name,
            "modelApiBase": self.model_api_base,
            "modelApiKey": self.model_api_key,
        }


@dataclass
class LangfuseConfiguration:
    """Langfuse 配置"""

    enabled: bool = True
    host: str = "http://localhost:3000"
    public_key: str = ""
    secret_key: str = ""
    project_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "publicKey": self.public_key,
            "secretKey": self.secret_key,
            "projectName": self.project_name,
        }


@dataclass
class ObservabilityConfiguration:
    """可观测性配置"""

    langfuse: Optional[LangfuseConfiguration] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        if self.langfuse:
            result["langfuse"] = self.langfuse.to_dict()
        return result


@dataclass
class LogCollectionConfiguration:
    """日志采集配置"""

    enabled: bool = False
    endpoint: str = ""
    project_name: str = ""
    pool_name: str = ""
    image: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "projectName": self.project_name,
            "poolName": self.pool_name,
            "image": self.image,
        }


@dataclass
class CreateAgentRuntimeInput:
    """CreateAgentRuntime 请求体"""

    agent_runtime_name: str
    artifact_type: ArtifactType
    description: str = ""
    cpu: float = 1.0
    memory: int = 4  # G (吉字节)
    port: int = 8080
    instance_count: int = 1
    session_concurrency_limit_per_instance: int = 10
    session_idle_timeout_seconds: int = 600
    code_configuration: Optional[CodeConfiguration] = None
    container_configuration: Optional[ContainerConfiguration] = None
    model_configuration: Optional[ModelConfiguration] = None
    observability: Optional[ObservabilityConfiguration] = None
    health_check_configuration: Optional[HealthCheckConfiguration] = None
    log_collection: Optional[LogCollectionConfiguration] = None
    environment_variables: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "agentRuntimeName": self.agent_runtime_name,
            "artifactType": self.artifact_type.value,
            "cpu": self.cpu,
            "memory": self.memory,
            "port": self.port,
            "instanceCount": self.instance_count,
            "sessionConcurrencyLimitPerInstance": self.session_concurrency_limit_per_instance,
            "sessionIdleTimeoutSeconds": self.session_idle_timeout_seconds,
        }

        if self.description:
            result["description"] = self.description

        if self.code_configuration:
            result["codeConfiguration"] = self.code_configuration.to_dict()

        if self.container_configuration:
            result["containerConfiguration"] = self.container_configuration.to_dict()

        if self.model_configuration:
            result["modelConfiguration"] = self.model_configuration.to_dict()

        if self.observability:
            result["observability"] = self.observability.to_dict()

        if self.health_check_configuration:
            result["healthCheckConfiguration"] = self.health_check_configuration.to_dict()

        if self.log_collection:
            result["logCollection"] = self.log_collection.to_dict()

        if self.environment_variables:
            result["environmentVariables"] = self.environment_variables

        return result


@dataclass
class CreateAgentRuntimeOutput:
    """CreateAgentRuntime 响应"""

    request_id: str = ""
    agent_runtime_id: str = ""
    agent_runtime_name: str = ""
    workspace_id: str = ""
    namespace_name: str = ""
    endpoint: str = ""
    status: str = ""
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CreateAgentRuntimeOutput":
        return cls(
            request_id=data.get("requestId", ""),
            agent_runtime_id=data.get("agentRuntimeId", ""),
            agent_runtime_name=data.get("agentRuntimeName", ""),
            workspace_id=data.get("workspaceId", ""),
            namespace_name=data.get("namespaceName", ""),
            endpoint=data.get("endpoint", ""),
            status=data.get("status", ""),
            created_at=data.get("createdAt", ""),
        )


@dataclass
class GetAgentRuntimeInput:
    """GetAgentRuntime 请求体"""

    agent_runtime_id: str = ""
    agent_runtime_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        if self.agent_runtime_id:
            result["agentRuntimeId"] = self.agent_runtime_id
        if self.agent_runtime_name:
            result["agentRuntimeName"] = self.agent_runtime_name
        return result


@dataclass
class GetAgentRuntimeOutput:
    """GetAgentRuntime 响应"""

    request_id: str = ""
    agent_runtime_id: str = ""
    agent_runtime_name: str = ""
    description: str = ""
    namespace_name: str = ""
    endpoint: str = ""
    status: str = ""
    phase: str = ""
    message: str = ""
    replicas: int = 0
    ready_replicas: int = 0
    langfuse_trace_url: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GetAgentRuntimeOutput":
        return cls(
            request_id=data.get("requestId", ""),
            agent_runtime_id=data.get("agentRuntimeId", ""),
            agent_runtime_name=data.get("agentRuntimeName", ""),
            description=data.get("description", ""),
            namespace_name=data.get("namespaceName", ""),
            endpoint=data.get("endpoint", ""),
            status=data.get("status", ""),
            phase=data.get("phase", ""),
            message=data.get("message", ""),
            replicas=data.get("replicas", 0),
            ready_replicas=data.get("readyReplicas", 0),
            langfuse_trace_url=data.get("langfuseTraceUrl", ""),
            created_at=data.get("createdAt", ""),
            updated_at=data.get("updatedAt", ""),
        )


@dataclass
class DeleteAgentRuntimeInput:
    """DeleteAgentRuntime 请求体"""

    agent_runtime_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"agentRuntimeId": self.agent_runtime_id}


@dataclass
class DeleteAgentRuntimeOutput:
    """DeleteAgentRuntime 响应"""

    request_id: str = ""
    agent_runtime_id: str = ""
    agent_runtime_name: str = ""
    status: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeleteAgentRuntimeOutput":
        return cls(
            request_id=data.get("requestId", ""),
            agent_runtime_id=data.get("agentRuntimeId", ""),
            agent_runtime_name=data.get("agentRuntimeName", ""),
            status=data.get("status", ""),
        )


@dataclass
class ListAgentRuntimesInput:
    """ListAgentRuntimes 请求体"""

    name: str = ""
    status: str = ""
    page_num: int = 1
    page_size: int = 20

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "pageNum": self.page_num,
            "pageSize": self.page_size,
        }
        if self.name:
            result["name"] = self.name
        if self.status:
            result["status"] = self.status
        return result


@dataclass
class ListAgentRuntimesOutput:
    """ListAgentRuntimes 响应"""

    request_id: str = ""
    agent_runtimes: List[GetAgentRuntimeOutput] = field(default_factory=list)
    total_count: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ListAgentRuntimesOutput":
        runtimes = []
        for item in data.get("agentRuntimes", []):
            runtimes.append(GetAgentRuntimeOutput.from_dict(item))
        return cls(
            request_id=data.get("requestId", ""),
            agent_runtimes=runtimes,
            total_count=data.get("totalCount", 0),
        )


# ============================================================================
# API 异常
# ============================================================================


class ServerlessAPIError(Exception):
    """Serverless API 错误"""

    def __init__(
        self, message: str, status_code: int = 0, request_id: str = "", response: Dict = None
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.response = response or {}


# ============================================================================
# API Client
# ============================================================================


class ServerlessAPIClient:
    """金山云 Serverless AgentRuntime API 客户端

    使用方式:
        client = ServerlessAPIClient(
            account_id="2000003485",
            region="cn-beijing-6",
            access_key_id="your-ak",
            secret_access_key="your-sk",
        )

        # 创建 AgentRuntime
        result = await client.create_agent_runtime(input)

        # 查询状态
        status = await client.get_agent_runtime(GetAgentRuntimeInput(agent_runtime_name="my-agent"))
    """

    def __init__(
        self,
        account_id: str = "",
        region: str = "cn-beijing-6",
        endpoint: str = "",
        timeout: float = 60.0,
        access_key_id: str = "",
        secret_access_key: str = "",
        service: str = "kmr",
    ):
        """初始化 API 客户端

        Args:
            account_id: 金山云账号 ID (从环境变量 KSYUN_ACCOUNT_ID 读取)
            region: 区域 (cn-beijing-6, cn-shanghai-1, cn-guangzhou-1)
            endpoint: API 端点 (默认使用内网预发环境)
            timeout: 请求超时时间
            access_key_id: 访问密钥 ID (从环境变量 KSYUN_ACCESS_KEY 读取)
            secret_access_key: 访问密钥 (从环境变量 KSYUN_SECRET_KEY 读取)
            service: 服务名称 (默认 kmr)
        """
        self.account_id = account_id or os.environ.get("KSYUN_ACCOUNT_ID", "")
        self.region = region
        self.endpoint = endpoint or os.environ.get(
            "SERVERLESS_ENDPOINT", DEFAULT_SERVERLESS_ENDPOINT
        )
        self.timeout = timeout
        self.service = service

        # AWS V4 签名凭证
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

        # 使用通用签名模块
        self._auth = AWSV4Auth(
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            region=self.region,
            service=self.service,
        )

        if self._auth.is_enabled:
            logger.debug(f"AWS4Auth initialized for service={service}, region={region}")

        # requests Session
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        """获取 requests Session"""
        if self._session is None:
            self._session = requests.Session()
            self._session.timeout = self.timeout
        return self._session

    async def close(self):
        """关闭客户端"""
        if self._session:
            self._session.close()
            self._session = None

    def _get_host(self) -> str:
        """从 endpoint 提取 host"""
        # 移除协议前缀
        host = self.endpoint
        if host.startswith("http://"):
            host = host[7:]
        elif host.startswith("https://"):
            host = host[8:]
        # 移除路径和尾部斜杠
        host = host.split("/")[0]
        return host.rstrip("/")
    
    def _is_kmr_api(self) -> bool:
        """检测是否是 KMR 风格的 API（需要 X-Action/X-Version 头）
        
        KMR API 格式: http://kmr.{region}.inner.api.ksyun.com
        旧 API 格式: http://serverless-spark-console.../serverless/v1
        """
        return "kmr." in self.endpoint and ".api.ksyun.com" in self.endpoint

    def _build_headers(self, request_id: str = "", action: str = "") -> Dict[str, str]:
        """构建请求头"""
        if not request_id:
            request_id = str(uuid.uuid4())

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": self._get_host(),
            "X-Ksc-Request-Id": request_id,
            "X-Ksc-Account-Id": self.account_id,
            "X-Ksc-Region": self.region,
        }
        
        # KMR API 需要额外的 X-Version 和 X-Action 头
        if self._is_kmr_api():
            headers["X-Version"] = "2025-07-21"
            if action:
                headers["X-Action"] = action
        
        return headers

    async def _request(
        self,
        path: str,
        body: Dict[str, Any],
        request_id: str = "",
    ) -> Dict[str, Any]:
        """发送 API 请求 (带 AWS V4 签名)"""
        # 从 path 提取 Action 名称 (如 /ListAgentRuntimes -> ListAgentRuntimes)
        action = path.lstrip("/").split("/")[-1] if path else ""
        
        headers = self._build_headers(request_id, action=action)

        # 完整 URL
        full_url = f"{self.endpoint}{path}"

        # 序列化请求体
        body_str = json.dumps(body, ensure_ascii=False)

        logger.debug(f"Request: POST {full_url}")
        logger.debug(f"Headers: {headers}")
        logger.debug(f"Body: {body_str}")

        try:
            session = self._get_session()

            # 发送请求 (带 AWS4Auth 签名)
            response = session.post(
                full_url,
                data=body_str.encode("utf-8"),
                headers=headers,
                auth=self._auth.get_auth(),  # AWS V4 签名
                timeout=self.timeout,
            )

            logger.debug(f"Response status: {response.status_code}")
            logger.debug(f"Response body: {response.text}")

            # 检查 HTTP 状态码
            if response.status_code != 200:
                raise ServerlessAPIError(
                    message=f"API request failed: HTTP {response.status_code}, {response.text}",
                    status_code=response.status_code,
                    request_id=headers.get("X-Ksc-Request-Id", ""),
                    response={"text": response.text},
                )

            # 解析响应
            data = response.json()

            # 检查标准响应格式 (Code/Message/Data)
            if "Code" in data:
                code = data.get("Code")
                if code != 0:
                    raise ServerlessAPIError(
                        message=data.get("Message", "Unknown error"),
                        status_code=code,
                        request_id=data.get("RequestId", headers.get("X-Ksc-Request-Id", "")),
                        response=data,
                    )

                # 如果成功，尝试返回 Data 字段
                if "Data" in data:
                    return data["Data"]

            # 检查旧格式业务错误 (如果有 error 字段)
            if "error" in data:
                error = data["error"]
                raise ServerlessAPIError(
                    message=error.get("message", str(error)),
                    status_code=error.get("code", 0),
                    request_id=headers.get("X-Ksc-Request-Id", ""),
                    response=data,
                )

            return data

        except requests.RequestException as e:
            raise ServerlessAPIError(
                message=f"Network error: {e}",
                request_id=headers.get("X-Ksc-Request-Id", ""),
            )

    # ========================================================================
    # API 方法
    # ========================================================================

    async def create_agent_runtime(
        self,
        input: CreateAgentRuntimeInput,
    ) -> CreateAgentRuntimeOutput:
        """创建 AgentRuntime

        Args:
            input: 创建请求

        Returns:
            创建结果
        """
        data = await self._request(API_CREATE_AGENT_RUNTIME, input.to_dict())
        return CreateAgentRuntimeOutput.from_dict(data)

    async def get_agent_runtime(
        self,
        input: GetAgentRuntimeInput,
    ) -> GetAgentRuntimeOutput:
        """获取 AgentRuntime 状态

        Args:
            input: 查询请求 (可用 agent_runtime_id 或 agent_runtime_name)

        Returns:
            AgentRuntime 详情
        """
        data = await self._request(API_GET_AGENT_RUNTIME, input.to_dict())
        return GetAgentRuntimeOutput.from_dict(data)

    async def delete_agent_runtime(
        self,
        input: DeleteAgentRuntimeInput,
    ) -> DeleteAgentRuntimeOutput:
        """删除 AgentRuntime

        Args:
            input: 删除请求

        Returns:
            删除结果
        """
        data = await self._request(API_DELETE_AGENT_RUNTIME, input.to_dict())
        return DeleteAgentRuntimeOutput.from_dict(data)

    async def list_agent_runtimes(
        self,
        input: ListAgentRuntimesInput = None,
    ) -> ListAgentRuntimesOutput:
        """列出 AgentRuntime

        Args:
            input: 列表请求 (可选，支持分页和过滤)

        Returns:
            AgentRuntime 列表
        """
        if input is None:
            input = ListAgentRuntimesInput()
        data = await self._request(API_LIST_AGENT_RUNTIMES, input.to_dict())
        return ListAgentRuntimesOutput.from_dict(data)


# ============================================================================
# 便捷函数
# ============================================================================


def get_client(
    account_id: str = "",
    region: str = "cn-beijing-6",
    access_key_id: str = "",
    secret_access_key: str = "",
) -> ServerlessAPIClient:
    """获取 API 客户端实例

    Args:
        account_id: 账号 ID (默认从环境变量读取)
        region: 区域
        access_key_id: 访问密钥 ID (默认从环境变量读取)
        secret_access_key: 访问密钥 (默认从环境变量读取)

    Returns:
        API 客户端
    """
    return ServerlessAPIClient(
        account_id=account_id,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
    )
