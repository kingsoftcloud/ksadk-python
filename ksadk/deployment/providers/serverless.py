"""
Serverless Provider - 金山云 Serverless 计算引擎 (AgentEngine 托管)

架构:
- Build 阶段: 客户端使用本地 AK/SK 直接上传代码包到 KS3
- Deploy 阶段: 客户端调用 AgentEngine Server API 发起部署
"""

import os
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
import click

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
from ksadk.api import AgentEngineClient


logger = logging.getLogger(__name__)


@DeployProviderRegistry.register("serverless")
class ServerlessProvider(DockerProvider):
    """金山云 Serverless 计算引擎 (AgentEngine Server 托管)"""

    name = "serverless"
    display_name = "AgentEngine Serverless (Managed)"
    description = "部署到金山云 Serverless (via AgentEngine Server)"

    supports_streaming = True
    supports_scaling = True
    requires_image_registry = False

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        # Serverless Provider 不再维护本地 API Client
        # 统一使用 AgentEngineClient (在方法中按需实例化或在此处初始化)
        pass

    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证配置: 确保已配置 AgentEngine Server"""
        
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        # token = os.getenv("AGENTENGINE_TOKEN") # Token 可选 (如果是内网或开发模式)
        
        if not server_url:
            # 默认回退到 localhost (与 AgentEngineClient 保持一致)
            # 但为了提示用户，我们可以打印一个警告，而不是报错
            click.echo("⚠️  未配置 AGENTENGINE_SERVER_URL，将使用默认值: http://localhost:8081")
            os.environ["AGENTENGINE_SERVER_URL"] = "http://localhost:8081"
        
        # 兼容性检查: 如果用户还在尝试用 Container 模式但没配 Registry
        artifact_type = target.extra.get("artifact_type", "Code")
        if artifact_type == "Container":
             docker_ok, docker_msg = await super().validate_config(target)
             if not docker_ok:
                 return docker_ok, docker_msg

        return True, ""

    async def package(self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None) -> PackageInfo:
        """打包项目: 复用父类逻辑，并恢复构建元数据"""
        # 复用父类 (DockerProvider) 的打包逻辑 (复制文件, 生成 Dockerfile 等)
        package_info = await super().package(project_dir, detection_result, config)
        
        # 尝试加载 build 命令保存的元数据 (如 ks3_path)
        try:
            metadata_file = Path(project_dir) / ".agentengine" / "build-metadata.json"
            if metadata_file.exists():
                import json
                with open(metadata_file, "r") as f:
                    saved_data = json.load(f)
                    if "metadata" in saved_data:
                         # 合并 metadata
                         count = 0
                         for k, v in saved_data["metadata"].items():
                             if k not in package_info.metadata or not package_info.metadata[k]:
                                 package_info.metadata[k] = v
                                 count += 1
                         
                         if count > 0:
                            click.echo(f"   📦 已加载上次构建元数据: {count} 项 (含 KS3 路径)")
        except Exception:
            pass
            
        return package_info



    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建 & 上传 (客户端直传 KS3)"""
        artifact_type = target.extra.get("artifact_type", "Code")

        if artifact_type == "Code":
            # 1. 检查是否已有 KS3 路径 (例如从外部传入)
            ks3_path = target.extra.get("ks3_path") or package_info.metadata.get("ks3_path")
            if ks3_path:
                package_info.metadata["ks3_path"] = ks3_path
                return package_info

            # 2. 构建 ZIP 包
            click.echo("\nStep 1/2: 构建代码包...")
            
            build_dir = Path(package_info.build_dir)
            dist_dir = build_dir.parent / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            zip_path = dist_dir / "code.zip"
            
            if not zip_path.exists(): 
                shutil.make_archive(str(zip_path.with_suffix("")), 'zip', root_dir=build_dir)
            
            # 3. 直接上传 KS3 (使用本地 AK/SK)
            click.echo("Step 2/2: 上传代码包到 KS3...")
            
            ks3_bucket = target.extra.get("ks3_bucket")
            upload_region = "cn-beijing-6" if target.region == "pre-online" else target.region
            
            uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
            object_key = f"agents/{package_info.name}/code.zip"
            
            ks3_path = await uploader.upload(zip_path, object_key)
            
            if not ks3_path:
                raise Exception("KS3 上传失败")
            
            logger.info(f"Upload success: {ks3_path}")
            package_info.metadata["ks3_path"] = ks3_path

        elif artifact_type == "Container":
             # Container 模式: 使用 DockerProvider 的 build 推送镜像
             return await super().build(package_info, target)
             
        return package_info

    async def deploy(self, package_info: PackageInfo, target: DeployTarget) -> DeployResult:
        """通过 AgentEngine Server 部署
        
        逻辑:
        - 读取本地 .agentengine.state 文件获取 agent_id
        - 如果有 agent_id → 执行更新 (endpoint 不变)
        - 如果没有 → 创建新 Agent，保存 agent_id 到状态文件
        """
        
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        click.echo(f"\n🚀 开始部署到 Serverless (Managed by {server_url})...")
        
        # 读取本地状态文件
        project_dir = package_info.project_dir
        state_file = Path(project_dir) / ".agentengine.state"
        local_state = self._load_state(state_file)
        existing_agent_id = local_state.get("agent_id")
        
        # 构造请求 payload
        artifact_type = target.extra.get("artifact_type", "Code")
        artifact_path = ""
        if artifact_type == "Code":
            artifact_path = package_info.metadata.get("ks3_path", "")
            
            # 转换为内网地址 (如果 serverless 无法解析 ks3://)
            if artifact_path.startswith("ks3://"):
                try:
                    from ksadk.common.constants import get_ks3_endpoints
                    
                    # 确定 KS3 Region code (pre-online -> cn-beijing)
                    ks3_region = target.extra.get("ks3_region")
                    if not ks3_region:
                         ks3_region = "cn-beijing-6" if target.region == "pre-online" else target.region
                    
                    _, internal_endpoint = get_ks3_endpoints(ks3_region)
                    
                    if internal_endpoint:
                        # ks3://bucket/key -> http://bucket.internal_endpoint/key
                        # 去掉 ks3://
                        parts = artifact_path.replace("ks3://", "").split("/", 1)
                        if len(parts) == 2:
                            bucket, key = parts
                            artifact_path = f"http://{bucket}.{internal_endpoint}/{key}"
                            click.echo(f"   Converted artifact path: {artifact_path}")
                except Exception as e:
                    logger.warning(f"Failed to convert ks3 path to internal URL: {e}")
                    
        else:
            artifact_path = package_info.image
        
        # 构建 KS3 凭证
        ks3_config = None
        if artifact_type == "Code":
            from ksadk.common.auth import AWSV4Auth
            auth = AWSV4Auth()  # 读取本地 AK/SK
            if auth.access_key_id and auth.secret_access_key:
                # 智能推断 Bucket 和 Region
                bucket_name = target.extra.get("ks3_bucket")
                if not bucket_name and artifact_path.startswith("ks3://"):
                    try:
                        # ks3://bucket/key -> bucket
                        bucket_name = artifact_path.split("/")[2]
                    except IndexError:
                        pass
                
                # Region 逻辑需与 Build 阶段保持一致 (pre-online -> cn-beijing-6)
                ks3_region = target.extra.get("ks3_region")
                if not ks3_region:
                     ks3_region = "cn-beijing-6" if target.region == "pre-online" else target.region

                ks3_config = {
                    "access_key": auth.access_key_id,
                    "secret_key": auth.secret_access_key,
                    "region": ks3_region,
                    "bucket": bucket_name,
                }
        
        try:
            async with AgentEngineClient() as client:
                if existing_agent_id:
                    # 有本地状态 → 执行更新
                    click.echo(f"   检测到本地状态: {existing_agent_id}")
                    click.echo(f"   执行热更新 (endpoint 保持不变)...")
                    
                    update_data = {
                        "artifact_path": artifact_path,
                        "resources": {
                            "cpu": target.resources.cpu,
                            "memory": target.resources.memory
                        },
                        "scaling": {
                            "min_replicas": target.scaling.min_replicas,
                            "max_replicas": target.scaling.max_replicas,
                            "concurrency": target.scaling.concurrency
                        },
                        "observability": {
                            "langfuse_enabled": target.extra.get("enable_observability", True)
                        }
                    }
                    
                    if ks3_config:
                        update_data["ks3"] = ks3_config
                    
                    res = await client.update_agent(existing_agent_id, update_data)
                    
                    # 更新本地状态
                    self._save_state(state_file, {
                        "agent_id": existing_agent_id,
                        "name": res.get("name"),
                        "region": target.region,
                        "endpoint": res.get("endpoint"),
                        "updated_at": self._now_iso(),
                    })
                    
                    return DeployResult(
                        status=DeployStatus.DEPLOYING, 
                        agent_id=existing_agent_id,
                        agent_name=res.get("name"),
                        endpoint=res.get("endpoint"),
                        message=f"✅ Agent 已更新: {existing_agent_id}"
                    )
                
                else:
                    # 没有本地状态 → 创建新 Agent
                    click.echo(f"   创建新 Agent: {package_info.name}")
                    
                    request_data = {
                        "name": package_info.name,
                        "framework": package_info.framework,
                        "artifact_type": artifact_type,
                        "artifact_path": artifact_path,
                        "region": target.region,
                        "resources": {
                            "cpu": target.resources.cpu,
                            "memory": target.resources.memory
                        },
                        "scaling": {
                            "min_replicas": target.scaling.min_replicas,
                            "max_replicas": target.scaling.max_replicas,
                            "concurrency": target.scaling.concurrency
                        },
                        "observability": {
                            "langfuse_enabled": target.extra.get("enable_observability", True)
                        }
                    }
                    
                    if ks3_config:
                        request_data["ks3"] = ks3_config

                    # 获取 Account ID (用于 Server 端的 user_id)
                    extra_headers = {}
                    ksyun_account_id = os.getenv("KSYUN_ACCOUNT_ID")
                    if ksyun_account_id:
                        extra_headers["X-Ksyun-Account-Id"] = ksyun_account_id
                    
                    # 注意: 这里需要重新实例化 client 以带上 extra_headers，或者我们应该一开始就带上
                    # 但上面的 client 已经被实例化了。
                    # 为了不破坏上面的 client 上下文，我们直接调用 client._request 的时候需要 headers
                    # AgentEngineClient.create_agent 并不接受 external headers.
                    # 所以我们需要修改 AgentEngineClient 的初始化。
                    
                    # 重新构造一个带 Header 的 client
                    async with AgentEngineClient(extra_headers=extra_headers) as new_client:
                        res = await new_client.create_agent(request_data)
                    
                    new_agent_id = res.get("agent_id")
                    
                    # 保存 agent_id 到本地状态文件
                    self._save_state(state_file, {
                        "agent_id": new_agent_id,
                        "name": res.get("name"),
                        "region": target.region,
                        "endpoint": res.get("endpoint"),
                        "api_key": res.get("api_key"),  # 只在首次保存
                        "created_at": self._now_iso(),
                    })
                    
                    click.echo(f"   💾 已保存状态到 .agentengine.state")
                    
                    return DeployResult(
                        status=DeployStatus.DEPLOYING, 
                        agent_id=new_agent_id,
                        agent_name=res.get("name"),
                        endpoint=res.get("endpoint"), 
                        api_key=res.get("api_key"),
                        message=f"✅ Agent ID: {new_agent_id} (首次部署)"
                    )
                
        except Exception as e:
            logger.error(f"Deploy failed: {e}")
            
            # 检测名称冲突
            err_msg = str(e)
            if "Conflict" in err_msg or "409" in err_msg or "already exists" in err_msg:
                return DeployResult(
                    status=DeployStatus.FAILED,
                    message=f"❌ 部署失败: Agent 名称 '{package_info.name}' 已存在。\n"
                            f"   提示: 请检查是否重复创建。\n"
                            f"   👉 解决方法: 请在 agentengine.yaml 中修改 'name' 字段 (如添加后缀) 后重试。"
                )
                
            return DeployResult(
                status=DeployStatus.FAILED,
                message=f"Server 请求失败: {str(e)}"
            )

    async def get_status(self, agent_id: str, target: DeployTarget) -> DeployResult:
        """获取 Agent 状态"""
        dry_run = target.extra.get("dry_run", False)
        try:
            from ksadk.common.auth import AWSV4Auth
            auth = AWSV4Auth()
            extra_headers = {}
            if auth.access_key_id and auth.secret_access_key:
                extra_headers["X-Ksyun-Access-Key"] = auth.access_key_id
                extra_headers["X-Ksyun-Secret-Key"] = auth.secret_access_key
                
            async with AgentEngineClient(dry_run=dry_run, extra_headers=extra_headers) as client:
                res = await client.get_agent(agent_id)
                
                status_map = {
                    "Running": DeployStatus.RUNNING,
                    "Ready": DeployStatus.RUNNING,
                    "Creating": DeployStatus.DEPLOYING,
                    "Updating": DeployStatus.UPDATING,
                    "Terminating": DeployStatus.STOPPING,
                    "Scaling": DeployStatus.UPDATING,
                    "Failed": DeployStatus.FAILED,
                    "Error": DeployStatus.FAILED,
                    "Unknown": DeployStatus.UNKNOWN
                }
                
                return DeployResult(
                    status=status_map.get(res.get("status"), DeployStatus.UNKNOWN),
                    agent_id=res.get("agent_id"),
                    agent_name=res.get("name"),
                    endpoint=res.get("endpoint"),
                    message=f"Status: {res.get('status')} ({res.get('phase')})"
                )
        except DryRunExit:
            return DeployResult(status=DeployStatus.SKIPPED, message="Dry Run executed.")
        except Exception as e:
            return DeployResult(
                status=DeployStatus.UNKNOWN,
                message=f"查询失败: {e}"
            )

    async def destroy(self, agent_id: str, target: DeployTarget) -> bool:
        """销毁 Agent"""
        dry_run = target.extra.get("dry_run", False)
        
        # 尝试清理本地状态文件
        state_file = Path(".") / ".agentengine.state"
        if state_file.exists():
            try:
                os.remove(state_file)
                logger.info(f"Deleted local state file: {state_file}")
            except Exception:
                pass

        try:
            from ksadk.common.auth import AWSV4Auth
            auth = AWSV4Auth()
            extra_headers = {}
            if auth.access_key_id and auth.secret_access_key:
                extra_headers["X-Ksyun-Access-Key"] = auth.access_key_id
                extra_headers["X-Ksyun-Secret-Key"] = auth.secret_access_key

            async with AgentEngineClient(dry_run=dry_run, extra_headers=extra_headers) as client:
                click.echo(f"正在通过 Server 删除 Agent: {agent_id}...")
                success = await client.delete_agent(agent_id)
                return success
        except DryRunExit:
            # 让异常冒泡给 CLI 处理
            raise
        except Exception as e:
            logger.error(f"Failed to delete agent: {e}")
            return False

    async def list_agents(self, target: DeployTarget) -> List[DeployResult]:
        """列出所有 Agent"""
        dry_run = target.extra.get("dry_run", False)
        try:
            from ksadk.common.auth import AWSV4Auth
            auth = AWSV4Auth()
            extra_headers = {}
            if auth.access_key_id and auth.secret_access_key:
                extra_headers["X-Ksyun-Access-Key"] = auth.access_key_id
                extra_headers["X-Ksyun-Secret-Key"] = auth.secret_access_key
                
            async with AgentEngineClient(dry_run=dry_run, extra_headers=extra_headers) as client:
                res = await client.list_agents()
                
                results = []
                for agent in res.get("Agents", []):
                    results.append(DeployResult(
                        status=DeployStatus.RUNNING if agent.get("status") == "Running" else DeployStatus.UNKNOWN,
                        agent_id=agent.get("agent_id"),
                        agent_name=agent.get("name"),
                        endpoint=agent.get("endpoint"),
                        message=agent.get("status")
                    ))
                return results
        except DryRunExit:
            raise
        except Exception as e:
            logger.error(f"List agents failed: {e}")
            return []

    async def invoke(self, agent_id: str, message: str, target: DeployTarget) -> str:
        """调用 Agent"""
        dry_run = target.extra.get("dry_run", False)
        try:
            async with AgentEngineClient(dry_run=dry_run) as client:
                response = await client.chat(agent_id, message, stream=False)
                return response.get("output", "")
        except DryRunExit:
            raise
        except Exception as e:
            return f"Error: {e}"

    def _load_state(self, state_file: Path) -> Dict[str, Any]:
        """读取本地状态文件"""
        import yaml
        if state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"Failed to load state file: {e}")
        return {}

    def _save_state(self, state_file: Path, state: Dict[str, Any]) -> None:
        """保存状态到本地文件"""
        import yaml
        try:
            with open(state_file, 'w', encoding='utf-8') as f:
                yaml.dump(state, f, default_flow_style=False, allow_unicode=True)
            logger.debug(f"State saved to {state_file}")
        except Exception as e:
            logger.warning(f"Failed to save state file: {e}")

    def _now_iso(self) -> str:
        """返回当前时间 ISO 格式"""
        from datetime import datetime
        return datetime.now().isoformat()

