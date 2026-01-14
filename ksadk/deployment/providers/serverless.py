"""
Serverless Provider - 金山云 Serverless 计算引擎 (AgentEngine 托管)

支持两种部署模式:
- Code: 代码包 (zip) 部署
- Container: 容器镜像部署
"""

import os
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from enum import Enum

from ksadk.builders.ks3_uploader import KS3Uploader
from ksadk.deployment.base import (
    BaseDeployProvider,
    DeployTarget,
    DeployResult,
    DeployStatus,
    PackageInfo,
)
from ksadk.deployment.registry import DeployProviderRegistry
from ksadk.deployment.providers.docker import DockerProvider
from ksadk.common.constants import get_ks3_endpoints
from ksadk.deployment.providers.serverless_api import (
    ServerlessAPIClient,
    ServerlessAPIError,
    CreateAgentRuntimeInput,
    GetAgentRuntimeInput,
    DeleteAgentRuntimeInput,
    ListAgentRuntimesInput,
    CodeConfiguration,
    ContainerConfiguration,
    ModelConfiguration,
    LangfuseConfiguration,
    ObservabilityConfiguration,
    ArtifactType,
)


logger = logging.getLogger(__name__)


# 状态映射: Serverless API 状态 -> DeployStatus
STATUS_MAPPING = {
    "Creating": DeployStatus.DEPLOYING,
    "Pending": DeployStatus.DEPLOYING,
    "Running": DeployStatus.RUNNING,
    "Ready": DeployStatus.RUNNING,
    "Healthy": DeployStatus.RUNNING,
    "Updating": DeployStatus.UPDATING,
    "Scaling": DeployStatus.UPDATING,
    "Stopping": DeployStatus.STOPPING,
    "Stopped": DeployStatus.STOPPED,
    "Failed": DeployStatus.FAILED,
    "Error": DeployStatus.FAILED,
    "Terminated": DeployStatus.STOPPED,
    "Deleting": DeployStatus.STOPPING,
}


@DeployProviderRegistry.register("serverless")
class ServerlessProvider(DockerProvider):
    """金山云 Serverless 计算引擎 (AgentEngine 托管)

    支持两种模式:
    - Code: 打包 zip + 依赖，上传 KS3，平台运行 python-slim 镜像
    - Container: 用户构建镜像，推送到 KCR
    """

    name = "serverless"
    display_name = "AgentEngine Serverless"
    description = "部署到金山云 Serverless 计算引擎 (支持 Code/Container 模式)"

    supports_streaming = True
    supports_scaling = True
    requires_image_registry = False  # Code 模式不需要

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._api_client: Optional[ServerlessAPIClient] = None

    def _get_api_client(self, target: DeployTarget) -> ServerlessAPIClient:
        """获取 API 客户端"""
        if self._api_client is None:
            account_id = target.extra.get("account_id") or os.environ.get("KSYUN_ACCOUNT_ID", "")
            region = target.region or "cn-beijing-6"

            # 获取 AK/SK 用于 AWS V4 签名
            access_key_id = (
                target.extra.get("access_key_id")
                or os.environ.get("KSYUN_ACCESS_KEY")
                or os.environ.get("KS3_ACCESS_KEY", "")
            )
            secret_access_key = (
                target.extra.get("secret_access_key")
                or os.environ.get("KSYUN_SECRET_KEY")
                or os.environ.get("KS3_SECRET_KEY", "")
            )

            self._api_client = ServerlessAPIClient(
                account_id=account_id,
                region=region,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
            )
        return self._api_client

    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证 Serverless 配置"""
        artifact_type = target.extra.get("artifact_type", "Code")  # 默认 Code 模式

        # 检查账号 ID
        account_id = target.extra.get("account_id") or os.environ.get("KSYUN_ACCOUNT_ID")
        if not account_id:
            return False, "需要金山云账号 ID (KSYUN_ACCOUNT_ID 环境变量或 --account-id 参数)"

        # Code 模式检查 KS3 凭证
        if artifact_type == "Code":
            ak = os.environ.get("KS3_ACCESS_KEY") or os.environ.get("KSYUN_ACCESS_KEY")
            sk = os.environ.get("KS3_SECRET_KEY") or os.environ.get("KSYUN_SECRET_KEY")
            if not ak or not sk:
                return False, "Code 模式需要 KS3 凭证 (KS3_ACCESS_KEY, KS3_SECRET_KEY)"

        # Container 模式检查 Docker
        elif artifact_type == "Container":
            docker_ok, docker_msg = await super().validate_config(target)
            if not docker_ok:
                return docker_ok, docker_msg

        return True, ""

    async def package(
        self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None
    ) -> PackageInfo:
        """打包项目 (根据模式选择)"""
        # 默认使用父类打包 (Docker)
        return await super().package(project_dir, detection_result, config)

    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建 (根据模式选择)"""
        artifact_type = target.extra.get("artifact_type", "Code")  # 默认 Code 模式

        if artifact_type == "Code":
            # Code 模式: 打包 zip 并上传 KS3
            # 如果已经通过 agentengine build --mode code 完成，package_info 中会包含 ks3_path
            ks3_path = target.extra.get("ks3_path") or package_info.metadata.get("ks3_path")

            if not ks3_path:
                logger.info("Code 模式: 开始打包并上传代码...")

                # 1. 创建 zip 包
                build_dir = Path(package_info.build_dir)
                dist_dir = build_dir.parent / "dist"
                dist_dir.mkdir(parents=True, exist_ok=True)
                zip_path = dist_dir / "code.zip"

                # 检查 build_dir 是否存在
                if not build_dir.exists():
                    raise RuntimeError(f"构建目录不存在: {build_dir}")

                logger.info(f"   打包代码: {zip_path}")
                shutil.make_archive(str(zip_path.with_suffix("")), "zip", build_dir)

                # 2. 上传到 KS3
                ak = os.environ.get("KS3_ACCESS_KEY") or os.environ.get("KSYUN_ACCESS_KEY")
                sk = os.environ.get("KS3_SECRET_KEY") or os.environ.get("KSYUN_SECRET_KEY")

                if not ak or not sk:
                    raise RuntimeError(
                        "Code 模式需要 KS3 凭证 (KSYUN_ACCESS_KEY, KSYUN_SECRET_KEY)"
                    )

                # KS3Uploader 从环境变量读取 AK/SK
                # bucket 默认为 agentengine-{region}，可通过 KS3_BUCKET 环境变量或 --ks3-bucket 参数自定义
                # 预发特殊逻辑: target.region 为 pre-online 时，资源上传到 cn-beijing-6
                upload_region = "cn-beijing-6" if target.region == "pre-online" else (target.region or "cn-beijing-6")
                
                bucket_name = target.extra.get("ks3_bucket")  # 从 CLI 参数获取
                
                uploader = KS3Uploader(region=upload_region, bucket=bucket_name)

                object_key = f"agents/{package_info.name}/code.zip"
                logger.info(f"   上传代码: ks3://{uploader.bucket}/{object_key}")

                ks3_path = await uploader.upload(zip_path, object_key)
                if not ks3_path:
                    raise RuntimeError("代码上传失败")

                logger.info(f"   上传成功: {ks3_path}")

            package_info.metadata["ks3_path"] = ks3_path
            return package_info
        else:
            # Container 模式: Docker build + push to KCR
            registry = target.extra.get("registry", "kcr.cn-beijing-6.ksyuncs.com")
            namespace = target.extra.get("image_namespace", "agent-engine")
            tag = target.extra.get("tag", "latest")

            target.extra["registry"] = registry
            target.extra["image_namespace"] = namespace
            target.extra["tag"] = tag

            return await super().build(package_info, target)

    async def deploy(self, package_info: PackageInfo, target: DeployTarget) -> DeployResult:
        """部署到 Serverless"""
        artifact_type = target.extra.get("artifact_type", "Code")  # 默认 Code 模式

        # 构建 CreateAgentRuntime 请求
        create_input = self._build_create_input(package_info, target, artifact_type)

        # 调用 Serverless API
        client = self._get_api_client(target)

        try:
            logger.info(f"Creating AgentRuntime: {package_info.name}")
            response = await client.create_agent_runtime(create_input)

            return DeployResult(
                status=STATUS_MAPPING.get(response.status, DeployStatus.DEPLOYING),
                agent_id=response.agent_runtime_id,
                agent_name=response.agent_runtime_name,
                endpoint=response.endpoint,
                message=f"AgentRuntime 创建成功，状态: {response.status}",
                metadata={
                    "artifact_type": artifact_type,
                    "workspace_id": response.workspace_id,
                    "namespace_name": response.namespace_name,
                    "region": target.region,
                    "request_id": response.request_id,
                    "created_at": response.created_at,
                },
            )
        except ServerlessAPIError as e:
            logger.error(f"Failed to create AgentRuntime: {e}")
            return DeployResult(
                status=DeployStatus.FAILED,
                agent_name=package_info.name,
                message=f"创建失败: {e.message}",
                metadata={
                    "error": str(e),
                    "request_id": e.request_id,
                    "response": e.response,
                },
            )
        finally:
            await client.close()

    def _build_create_input(
        self, package_info: PackageInfo, target: DeployTarget, artifact_type: str
    ) -> CreateAgentRuntimeInput:
        """构建 CreateAgentRuntime 请求体"""
        ak = os.environ.get("KS3_ACCESS_KEY") or os.environ.get("KSYUN_ACCESS_KEY") or ""
        sk = os.environ.get("KS3_SECRET_KEY") or os.environ.get("KSYUN_SECRET_KEY") or ""

        # 解析资源配置
        cpu = self._parse_cpu(target.resources.cpu)
        memory = self._parse_memory(target.resources.memory)

        # 基础配置
        input_obj = CreateAgentRuntimeInput(
            agent_runtime_name=package_info.name,
            artifact_type=ArtifactType.CODE if artifact_type == "Code" else ArtifactType.CONTAINER,
            description=target.extra.get("description", f"{package_info.framework} Agent"),
            cpu=cpu,
            memory=memory,
            port=target.extra.get("port", 8000),
            instance_count=target.scaling.min_replicas,
            session_concurrency_limit_per_instance=target.scaling.concurrency,
            session_idle_timeout_seconds=target.extra.get("idle_timeout", 600),
        )

        # Code 模式配置
        if artifact_type == "Code":
            ks3_path = package_info.metadata.get("ks3_path", "")
            # 解析 ks3://bucket/path 格式
            if ks3_path.startswith("ks3://"):
                parts = ks3_path[6:].split("/", 1)
                bucket_name = parts[0]
                object_name = parts[1] if len(parts) > 1 else ""
            else:
                # 默认 Bucket 逻辑: agentengine-{region}
                target_region = target.region or "cn-beijing-6"
                upload_region = "cn-beijing-6" if target_region == "pre-online" else target_region
                default_bucket = f"agentengine-{upload_region}"
                
                bucket_name = target.extra.get("ks3_bucket") or default_bucket
                object_name = target.extra.get("ks3_object") or f"agents/{package_info.name}/code.zip"

            # 确定 KS3 Endpoint (优先内网)
            # 如果未指定，使用 region 映射的内网 Endpoint
            ks3_endpoint = os.environ.get("KS3_ENDPOINT")
            if not ks3_endpoint:
                _, internal_ep = get_ks3_endpoints(target.region)
                ks3_endpoint = internal_ep

            input_obj.code_configuration = CodeConfiguration(
                ks3_bucket_name=bucket_name,
                ks3_object_name=object_name,
                ks3_region=target.region,
                ks3_access_key=ak,
                ks3_secret_key=sk,
                ks3_endpoint=ks3_endpoint,
                language="python",
                command=[
                    "python",
                    "-m",
                    "uvicorn",
                    "entrypoint:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ],
            )

        # Container 模式配置
        else:
            input_obj.container_configuration = ContainerConfiguration(
                image=package_info.image or f"agentengine/{package_info.name}:latest",
                command=["python", "entrypoint.py"],
            )

        # 模型配置 (可选)
        model_name = os.environ.get("MODEL_NAME")
        model_api_base = os.environ.get("OPENAI_API_BASE") or os.environ.get("MODEL_API_BASE")
        model_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MODEL_API_KEY")

        if model_name and model_api_base and model_api_key:
            input_obj.model_configuration = ModelConfiguration(
                model_name=model_name,
                model_api_base=model_api_base,
                model_api_key=model_api_key,
            )

        # Langfuse 配置 (可选)
        enable_observability = target.extra.get("enable_observability", True)

        langfuse_host = os.environ.get("LANGFUSE_BASE_URL")
        langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

        # 只有在启用且有密钥的情况下才配置
        if enable_observability:
            # 即使本地没有密钥，也发送启用配置 (Serverless 平台通过环境变量自动注入)
            input_obj.observability = ObservabilityConfiguration(
                langfuse=LangfuseConfiguration(
                    enabled=True,
                    host=langfuse_host or "http://localhost:3000",
                    public_key=langfuse_public_key or "",
                    secret_key=langfuse_secret_key or "",
                )
            )

        # 环境变量 (从 target.extra 中提取)
        env_vars = target.extra.get("environment_variables", {})
        if env_vars:
            input_obj.environment_variables = env_vars

        return input_obj

    def _parse_cpu(self, cpu_str: str) -> float:
        """解析 CPU 配置 (支持 '2', '2000m' 格式)"""
        if "m" in cpu_str:
            return float(cpu_str.replace("m", "")) / 1000
        return float(cpu_str)

    def _parse_memory(self, memory_str: str) -> int:
        """解析内存配置 (支持 '4Gi', '4096Mi' 格式), 返回 G"""
        if "Gi" in memory_str:
            return int(memory_str.replace("Gi", ""))
        elif "Mi" in memory_str:
            # 向上取整，如 512Mi -> 1G
            mi_val = int(memory_str.replace("Mi", ""))
            return max(1, (mi_val + 1023) // 1024)
        return int(memory_str)

    async def get_status(self, agent_id: str, target: DeployTarget) -> DeployResult:
        """获取 Serverless Agent 状态"""
        client = self._get_api_client(target)

        try:
            # 支持通过 ID 或名称查询
            input_obj = GetAgentRuntimeInput(
                agent_runtime_id=agent_id if agent_id.startswith("ar-") else "",
                agent_runtime_name=agent_id if not agent_id.startswith("ar-") else "",
            )

            logger.info(f"Getting AgentRuntime status: {agent_id}")
            response = await client.get_agent_runtime(input_obj)

            return DeployResult(
                status=STATUS_MAPPING.get(response.status, DeployStatus.UNKNOWN),
                agent_id=response.agent_runtime_id,
                agent_name=response.agent_runtime_name,
                endpoint=response.endpoint,
                message=response.message or f"状态: {response.status}, 阶段: {response.phase}",
                metadata={
                    "phase": response.phase,
                    "replicas": response.replicas,
                    "ready_replicas": response.ready_replicas,
                    "langfuse_trace_url": response.langfuse_trace_url,
                    "namespace_name": response.namespace_name,
                    "created_at": response.created_at,
                    "updated_at": response.updated_at,
                    "request_id": response.request_id,
                },
            )
        except ServerlessAPIError as e:
            logger.error(f"Failed to get AgentRuntime status: {e}")
            return DeployResult(
                status=DeployStatus.UNKNOWN,
                agent_id=agent_id,
                message=f"查询失败: {e.message}",
                metadata={
                    "error": str(e),
                    "request_id": e.request_id,
                },
            )
        finally:
            await client.close()

    async def destroy(self, agent_id: str, target: DeployTarget) -> bool:
        """删除 Serverless Agent"""
        client = self._get_api_client(target)

        try:
            input_obj = DeleteAgentRuntimeInput(agent_runtime_id=agent_id)

            logger.info(f"Deleting AgentRuntime: {agent_id}")
            response = await client.delete_agent_runtime(input_obj)

            logger.info(
                f"AgentRuntime deleted: {response.agent_runtime_name}, status: {response.status}"
            )
            return True

        except ServerlessAPIError as e:
            logger.error(f"Failed to delete AgentRuntime: {e}")
            return False
        finally:
            await client.close()

    async def list_agents(self, target: DeployTarget) -> List[DeployResult]:
        """列出所有 Serverless Agent"""
        client = self._get_api_client(target)

        try:
            input_obj = ListAgentRuntimesInput(
                page_num=0,
                page_size=1000,  # 最多返回 1000 个
            )

            logger.info("Listing AgentRuntimes")
            response = await client.list_agent_runtimes(input_obj)

            results = []
            for runtime in response.agent_runtimes:
                results.append(
                    DeployResult(
                        status=STATUS_MAPPING.get(runtime.status, DeployStatus.UNKNOWN),
                        agent_id=runtime.agent_runtime_id,
                        agent_name=runtime.agent_runtime_name,
                        endpoint=runtime.endpoint,
                        message=runtime.message or f"状态: {runtime.status}",
                        metadata={
                            "phase": runtime.phase,
                            "replicas": runtime.replicas,
                            "ready_replicas": runtime.ready_replicas,
                            "created_at": runtime.created_at,
                        },
                    )
                )

            return results

        except ServerlessAPIError as e:
            logger.error(f"Failed to list AgentRuntimes: {e}")
            return []
        finally:
            await client.close()
