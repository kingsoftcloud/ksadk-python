"""
Serverless Provider - 金山云 Serverless 计算引擎 (AgentEngine 托管)

架构:
- Build 阶段: 客户端使用本地 AK/SK 直接上传代码包到 KS3
- Deploy 阶段: 客户端调用 AgentEngine Server API 发起部署
"""

import os
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import click

from ksadk.builders.ks3_uploader import KS3Uploader
from ksadk.builders.code_builder import CodeBuilder
from ksadk.deployment.agent_access import get_latest_agent_access
from ksadk.deployment.base import (
    BaseDeployProvider,
    DeployTarget,
    DeployResult,
    DeployStatus,
    PackageInfo,
)
from ksadk.deployment.registry import DeployProviderRegistry
from ksadk.deployment.ui_config import resolve_ui_config, ui_config_to_state_fields
from ksadk.builders.container_builder import ContainerBuilder
from ksadk.api import AgentEngineClient, DryRunExit


logger = logging.getLogger(__name__)


@DeployProviderRegistry.register("serverless")
@DeployProviderRegistry.register("kcf")
@DeployProviderRegistry.register("kce")
class ServerlessProvider(BaseDeployProvider):
    """金山云 Serverless 计算引擎 (AgentEngine Server 托管)
    
    重构后直接继承 BaseDeployProvider，不再依赖 DockerProvider。
    Container 模式使用 ContainerBuilder 进行打包和构建。
    """

    name = "serverless"
    display_name = "AgentEngine Serverless (Managed)"
    description = "部署到金山云 Serverless (via AgentEngine Server)"

    supports_streaming = True
    supports_scaling = True
    requires_image_registry = False

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    @staticmethod
    def _parse_code_artifact_path(artifact_path: str) -> tuple[Optional[str], Optional[str]]:
        """解析代码包路径，返回 (bucket, object_key)。"""
        if not artifact_path:
            return None, None

        path = artifact_path.strip()
        if not path:
            return None, None

        if path.startswith("ks3://"):
            raw = path[6:]
            parts = raw.split("/", 1)
            bucket = parts[0].strip() if parts else ""
            object_key = parts[1].strip() if len(parts) > 1 else ""
            return bucket or None, object_key or None

        if path.startswith("http://") or path.startswith("https://"):
            from urllib.parse import urlparse

            parsed = urlparse(path)
            bucket = (parsed.netloc.split(".", 1)[0] if parsed.netloc else "").strip()
            object_key = parsed.path.lstrip("/").strip()
            return bucket or None, object_key or None

        if "/" in path:
            bucket, object_key = path.split("/", 1)
            return bucket.strip() or None, object_key.strip() or None

        return None, None

    @staticmethod
    def _load_project_env_vars(env_file: Path) -> Dict[str, str]:
        """读取项目 .env，并修正 UTF-8 BOM 导致的脏 key。"""
        from dotenv import dotenv_values

        raw_env = dotenv_values(env_file, encoding="utf-8-sig")
        env_vars: Dict[str, str] = {}
        for key, value in raw_env.items():
            if not key or value is None:
                continue
            clean_key = str(key).lstrip("\ufeff").strip()
            if not clean_key:
                continue
            env_vars[clean_key] = str(value)
        return env_vars

    @staticmethod
    def _persist_build_metadata(package_info: PackageInfo) -> None:
        """持久化最近一次成功构建的制品信息，供后续命中缓存。"""
        metadata_file = Path(package_info.project_dir) / ".agentengine" / "build-metadata.json"
        metadata_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "name": package_info.name,
            "framework": package_info.framework,
            "build_dir": package_info.build_dir,
            "project_dir": package_info.project_dir,
            "entry_point": package_info.entry_point,
            "image": package_info.image,
            "metadata": package_info.metadata,
        }

        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _serialize_network_config(target: DeployTarget) -> Optional[Dict[str, Any]]:
        network = getattr(target, "network", None)
        if network is None:
            return None

        payload: Dict[str, Any] = {
            "enable_public_access": bool(getattr(network, "enable_public_access", False)),
            "enable_vpc_access": bool(getattr(network, "enable_vpc_access", False)),
        }

        for field in ("vpc_id", "subnet_id", "security_group_id", "availability_zone"):
            value = str(getattr(network, field, "") or "").strip()
            if value:
                payload[field] = value

        if not payload["enable_public_access"] and not payload["enable_vpc_access"] and len(payload) == 2:
            return None
        return payload

    @staticmethod
    def _serialize_storage_config(target: DeployTarget) -> Optional[Dict[str, Any]]:
        storage = getattr(target, "storage", None)
        if storage is None:
            return None

        mount_path = str(getattr(storage, "mount_path", "") or "").strip()
        size_gi = getattr(storage, "size_gi", None)
        if not mount_path and size_gi is None:
            return None

        payload: Dict[str, Any] = {}
        if mount_path:
            payload["mount_path"] = mount_path
        if size_gi is not None:
            payload["size_gi"] = int(size_gi)
        return payload

    @staticmethod
    def _format_elapsed(started_at: float) -> str:
        elapsed = max(0.0, time.monotonic() - started_at)
        if elapsed < 60:
            return f"{elapsed:.1f}s"
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return f"{minutes}m{seconds:02d}s"


    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证配置: 确保已配置 AgentEngine Server"""
        
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        
        if not server_url:
            click.echo("⚠️  未配置 AGENTENGINE_SERVER_URL，将尝试使用默认 Region 配置")
        
        # Container 模式: 检查 Docker 是否可用
        artifact_type = target.extra.get("artifact_type", "Code")
        if artifact_type == "Container":
            import subprocess
            try:
                result = subprocess.run(["docker", "version"], capture_output=True, timeout=10)
                if result.returncode != 0:
                    return False, "Docker 未正常运行"
            except FileNotFoundError:
                return False, "未找到 docker 命令，请确保已安装 Docker"
            except Exception as e:
                return False, f"Docker 检查失败: {e}"

        return True, ""

    async def package(self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None) -> PackageInfo:
        """打包项目
        
        Code 模式: 返回基本 PackageInfo，实际构建在 build() 中进行
        Container 模式: 使用 ContainerBuilder 打包
        """
        project_path = Path(project_dir)
        
        # 创建基本 PackageInfo
        package_info = PackageInfo(
            name=detection_result.name or project_path.name,
            framework=detection_result.type.value,
            build_dir=str(project_path / ".agentengine" / "build"),
            project_dir=str(project_path),
            entry_point=detection_result.entry_point,
            metadata={}
        )
        
        # 尝试加载之前保存的构建元数据 (ks3_path 等)
        try:
            metadata_file = project_path / ".agentengine" / "build-metadata.json"
            if metadata_file.exists():
                import json
                with open(metadata_file, "r") as f:
                    saved_data = json.load(f)
                    if "metadata" in saved_data:
                        count = 0
                        for k, v in saved_data["metadata"].items():
                            if v:
                                package_info.metadata[k] = v
                                count += 1
                        if count > 0:
                            click.echo(f"   📦 已加载上次构建元数据: {count} 项")
        except Exception as e:
            click.secho(f"⚠️  加载构建元数据失败: {e}", fg="yellow")
            
        return package_info



    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建 & 上传 (客户端直传 KS3)"""
        artifact_type = target.extra.get("artifact_type", "Code")

        if artifact_type == "Code":
            # 1. 检查是否已有 KS3 路径
            # 优先级: CLI传入 > Metadata缓存 (仅当 !no_cache)
            cli_ks3_path = target.extra.get("ks3_path")
            cached_ks3_path = package_info.metadata.get("ks3_path")
            no_cache = target.extra.get("no_cache", False)
            repackage = target.extra.get("repackage", False)
            
            # 如果显式传入了 ks3-path，直接使用
            if cli_ks3_path:
                package_info.metadata["ks3_path"] = cli_ks3_path
                return package_info
            
            # 如果没有 no_cache 且有缓存，才使用缓存
            if not no_cache and not repackage and cached_ks3_path:
                logger.info(f"Using cached bundle: {cached_ks3_path}")
                return package_info

            # 2. 构建 ZIP 包 (委托给 CodeBuilder)
            
            # 传递配置，包括 no_cache
            builder_config = target.extra.copy()
            builder_config["no_cache"] = no_cache
            builder_config["repackage"] = repackage
            
            # 实例化 Builder
            # 注意: CodeBuilder 目前设计为直接操作 project_dir，
            # 这里传入原始 project_dir (package_info.project_dir)
            builder = CodeBuilder(Path(package_info.project_dir), config=builder_config)
            
            # 执行构建
            build_result = builder.build()
            
            if not build_result.success:
                raise Exception(f"构建失败: {build_result.error_message}")
                
            zip_path = build_result.artifact_path
            # click.echo(f"   ✅ ZIP 已生成: {zip_path}")
            
            # 3. 直接上传 KS3 (使用本地 AK/SK)
            # 3. 直接上传 KS3 (使用本地 AK/SK)
            click.echo("\n正在上传代码包到 KS3...")
            upload_started_at = time.monotonic()
            
            ks3_bucket = target.extra.get("ks3_bucket")
            upload_region = "cn-beijing-6" if target.region == "pre-online" else target.region
            
            uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
            
            # 使用时间戳确保每次上传的代码包路径唯一，支持真正的版本回滚
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            object_key = f"agents/{package_info.name}/code_{timestamp}.zip"
            
            ks3_path = await uploader.upload(zip_path, object_key)
            
            if not ks3_path:
                raise Exception("KS3 上传失败")
            
            logger.info(f"Upload success: {ks3_path}")
            click.echo(f"   ✓ 上传耗时: {self._format_elapsed(upload_started_at)}")
            package_info.metadata["ks3_path"] = ks3_path

        elif artifact_type == "Container":
            # Container 模式: 使用 ContainerBuilder 构建并推送镜像
            cli_image = target.extra.get("image") or package_info.image
            if cli_image:
                package_info.image = cli_image
                package_info.metadata["image"] = cli_image
                self._persist_build_metadata(package_info)
                return package_info

            registry = target.extra.get("registry", "")
            tag = target.extra.get("tag", "latest")
            no_cache = target.extra.get("no_cache", False)
            cached_image = package_info.metadata.get("image")

            if not no_cache and cached_image:
                logger.info(f"Using cached image: {cached_image}")
                package_info.image = cached_image
                return package_info
            
            builder = ContainerBuilder(
                project_dir=Path(package_info.project_dir),
                tag=tag,
                registry=registry,
                no_cache=no_cache
            )
            
            # 构建镜像
            build_result = builder.build()
            if not build_result.success:
                raise Exception(f"镜像构建失败: {build_result.error_message}")
            
            image_name = build_result.metadata.get("image")
            package_info.image = image_name
            package_info.metadata["image"] = image_name
            
            # 推送镜像
            if not builder.push(image_name):
                raise Exception("镜像推送失败")
            
            click.secho(f"✅ 镜像已推送: {image_name}", fg="green")

        self._persist_build_metadata(package_info)
             
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
        resolved_ui = resolve_ui_config(
            framework=package_info.framework,
            state=local_state,
            cli_profile=target.extra.get("ui_profile"),
            cli_path=target.extra.get("ui_path"),
            cli_url=target.extra.get("ui_url"),
        )
        ui_state = ui_config_to_state_fields(resolved_ui)
        
        # 构造请求 payload
        artifact_type = target.extra.get("artifact_type", "Code")
        artifact_path = ""
        if artifact_type == "Code":
            original_artifact_path = package_info.metadata.get("ks3_path", "")
            artifact_path = original_artifact_path
            
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
                        bucket, key = self._parse_code_artifact_path(artifact_path)
                        if bucket and key:
                            artifact_path = f"http://{bucket}.{internal_endpoint}/{key}"
                            click.echo(f"   Converted artifact path: {artifact_path}")
                except Exception as e:
                    logger.warning(f"Failed to convert ks3 path to internal URL: {e}")
            
            if not artifact_path:
                raise ValueError(
                    "❌ 未找到代码包路径 (ks3_path)。\n"
                    "   在 Serverless 模式下，必须先上传代码包。\n"
                    "   👉 请先执行: agentengine build --mode code --push\n"
                    "   或者在 deploy 命令中使用 --ks3-path 指定路径。"
                )

            parsed_bucket, parsed_object_key = self._parse_code_artifact_path(original_artifact_path)
            if not parsed_bucket or not parsed_object_key:
                raise ValueError(
                    "❌ ks3_path 格式无效，缺少代码包对象路径。\n"
                    f"   当前值: {original_artifact_path or '(empty)'}\n"
                    "   期望格式: ks3://<bucket>/<object-key>\n"
                    "   👉 请重新执行: agentengine build --mode code --push\n"
                    "   或者传入完整的 --ks3-path。"
                )
                    
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
                if not bucket_name:
                    bucket_name, _ = self._parse_code_artifact_path(
                        package_info.metadata.get("ks3_path", "")
                    )
                
                # 如果没有提供，尝试从 artifact_path 解析
                if not bucket_name:
                    if artifact_path.startswith("ks3://"):
                        try:
                            # ks3://bucket/key -> bucket
                            bucket_name = artifact_path.split("/")[2]
                            logger.info(f"Extracted bucket from ks3:// path: {bucket_name}")
                        except IndexError:
                            pass
                    elif artifact_path.startswith("http")  and "." in artifact_path:
                        try:
                            # http://bucket.endpoint/key -> bucket
                            from urllib.parse import urlparse
                            parsed = urlparse(artifact_path)
                            bucket_name = parsed.netloc.split(".")[0]
                            logger.info(f"Extracted bucket from HTTP URL: {bucket_name}")
                        except Exception as e:
                            logger.warning(f"Failed to extract bucket from URL: {e}")
                
                # 如果仍然没有，使用智能默认值（与 KS3Uploader 逻辑一致）
                if not bucket_name:
                    account_id = os.getenv("KSYUN_ACCOUNT_ID")
                    upload_region = "cn-beijing-6" if target.region == "pre-online" else target.region
                    
                    if not account_id:
                        raise ValueError(
                            "❌ 缺少 KSYUN_ACCOUNT_ID 环境变量\n"
                            "   Bucket 名称格式必须为: agentengine-{account_id}-{region}\n"
                            "   请在 .env 文件中设置: KSYUN_ACCOUNT_ID=你的账号ID"
                        )
                    
                    bucket_name = f"agentengine-{account_id}-{upload_region}"
                    logger.info(f"Using default bucket: {bucket_name}")
                
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
                logger.info(f"📦 KS3 Config: bucket={bucket_name}, region={ks3_region}")
        
        try:
            # 获取 dry_run 标识
            is_dry_run = target.extra.get("dry_run", False)

            async with AgentEngineClient(region=target.region, dry_run=is_dry_run) as client:
                agent_exists = False
                
                if existing_agent_id:
                    # 有本地状态 → 先检查服务器上是否存在
                    click.echo(f"   检测到本地状态: {existing_agent_id}")
                    
                    try:
                        # 尝试获取 agent，确认是否存在
                        existing_agent = await client.get_agent(existing_agent_id)
                        if existing_agent:
                            agent_exists = True
                    except Exception as e:
                        # Agent 不存在或查询失败
                        err_msg = str(e).lower()
                        if "not found" in err_msg or "404" in err_msg or "不存在" in err_msg:
                            click.secho(f"   ⚠️  服务器上未找到 Agent {existing_agent_id}，将创建新 Agent", fg="yellow")
                            agent_exists = False
                        # 如果是 DryRun 抛出的异常（表明请求本来会发出去但被拦截了），我们认为 Agent 可能存在也可能不存在
                        # 但为了安全起见，在 DryRun 模式下我们假设它存在并走更新路径，或者简单地打印日志
                        elif "Dry Run" in str(e):
                             click.secho(f"   [Dry Run] 假设 Agent {existing_agent_id} 存在", fg="cyan")
                             agent_exists = True
                        else:
                            # 其他错误，重新抛出
                            raise
                    
                    if agent_exists:
                        # Agent 存在 → 执行更新
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
                            },
                            "ui_config": {
                                "profile": resolved_ui.profile,
                                "path": resolved_ui.path,
                                "url": resolved_ui.url,
                            },
                        }
                        
                        if ks3_config:
                            update_data["ks3"] = ks3_config

                        # 加载本地 .env 并注入到环境变量 (更新时也同步)
                        env_file = Path(project_dir) / ".env"
                        env_vars = {}
                        if env_file.exists():
                             env_vars = self._load_project_env_vars(env_file)
                             if env_vars:
                                 update_data["env_vars"] = env_vars
                                 click.echo(f"   📦 更新环境变量: {len(env_vars)} 项 from .env")

                        network_config = self._serialize_network_config(target)
                        if network_config:
                            update_data["network"] = network_config

                        storage_config = self._serialize_storage_config(target)
                        if storage_config:
                            update_data["storage"] = storage_config
                        
                        # 注入更新时间戳，强制触发 Rolling Update (Pod 重启)
                        if "env_vars" not in update_data:
                            update_data["env_vars"] = {}
                        
                        import time
                        update_data["env_vars"]["KSADK_UPDATED_AT"] = str(int(time.time()))
                        
                        # DEBUG: 打印 Payload 确认 trigger 是否存在
                        click.echo(f"   🔄 更新 Trigger: KSADK_UPDATED_AT={update_data['env_vars']['KSADK_UPDATED_AT']}")
                        
                        res = await client.update_agent(existing_agent_id, update_data)
                        latest_access = {}
                        if not is_dry_run:
                            latest_access = await get_latest_agent_access(
                                client,
                                agent_id=existing_agent_id,
                                include_api_key=True,
                                on_error=lambda exc: logger.warning(
                                    "Failed to refresh quick access for %s: %s",
                                    existing_agent_id,
                                    exc,
                                ),
                            )
                        
                        # 如果是 Dry Run，手动构造假响应以避免崩溃
                        if is_dry_run and not res:
                            res = {"name": package_info.name, "endpoint": "http://dry-run-endpoint"}
                        
                        # 更新本地状态 (保留旧字段如 api_key)
                        new_state = local_state.copy()
                        new_state.update({
                            "agent_id": latest_access.get("agent_id") or existing_agent_id,
                            "name": latest_access.get("name") or res.get("name"),
                            "region": target.region,
                            "endpoint": latest_access.get("endpoint") or res.get("endpoint"),
                            "updated_at": self._now_iso(),
                            **ui_state,
                        })
                        if latest_access.get("api_key"):
                            new_state["api_key"] = latest_access["api_key"]
                        self._save_state(state_file, new_state)
                        
                        return DeployResult(
                            status=DeployStatus.DEPLOYING, 
                            agent_id=existing_agent_id,
                            agent_name=res.get("name"),
                            endpoint=res.get("endpoint"),
                            message=f"✅ Agent 已更新: {existing_agent_id}"
                        )
                    # else: agent_exists = False，继续执行创建逻辑
                
                # 没有本地状态，或本地状态对应的 Agent 在服务器上不存在 → 创建新 Agent
                if not existing_agent_id or not agent_exists:
                    click.echo(f"   创建新 Agent: {package_info.name}")
                    
                    request_data = {
                        "name": package_info.name,
                        "framework": package_info.framework,
                        "artifact_type": artifact_type,
                        "artifact_path": artifact_path,
                        "region": target.region,
                        "instance_id": target.extra.get("instance_id", "default"),
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
                        },
                        "ui_config": {
                            "profile": resolved_ui.profile,
                            "path": resolved_ui.path,
                            "url": resolved_ui.url,
                        },
                    }
                    
                    if ks3_config:
                        request_data["ks3"] = ks3_config

                    # Container 模式: 传递镜像凭证
                    if artifact_type == "Container":
                        # KCR_USERNAME 默认使用 KSYUN_ACCOUNT_ID
                        kcr_username = os.getenv("KCR_USERNAME", "") or os.getenv("KSYUN_ACCOUNT_ID", "")
                        kcr_password = os.getenv("KCR_PASSWORD")
                        kcr_endpoint = os.getenv("KCR_ENDPOINT", "ghcr.io")

                        if kcr_username and kcr_password:
                            request_data["image_credential"] = {
                                "endpoint": kcr_endpoint,
                                "username": kcr_username,
                                "password": kcr_password,
                            }
                            click.echo(f"   🔑 镜像凭证: {kcr_username}@{kcr_endpoint}")
                        else:
                            click.secho("   ⚠️  未配置镜像凭证 (KCR_USERNAME/KCR_PASSWORD)，私有镜像可能无法拉取", fg="yellow")

                    # 加载本地 .env 并注入到环境变量
                    env_file = Path(project_dir) / ".env"
                    env_vars = {}
                    if env_file.exists():
                        env_vars = self._load_project_env_vars(env_file)
                        click.echo(f"   📦 加载环境变量: {len(env_vars)} 项 from .env")
                    
                    if env_vars:
                         request_data["env_vars"] = env_vars

                    network_config = self._serialize_network_config(target)
                    if network_config:
                        request_data["network"] = network_config

                    storage_config = self._serialize_storage_config(target)
                    if storage_config:
                        request_data["storage"] = storage_config

                    # 获取 Account ID (用于 Server 端的 user_id)
                    extra_headers = {}
                    ksyun_account_id = os.getenv("KSYUN_ACCOUNT_ID")
                    if ksyun_account_id:
                        extra_headers["X-Ksc-Account-Id"] = ksyun_account_id
                    
                    # 重新构造一个带 Header 的 client
                    async with AgentEngineClient(region=target.region, extra_headers=extra_headers, dry_run=is_dry_run) as new_client:
                        res = await new_client.create_agent(request_data)
                    
                    # 如果是 Dry Run，手动构造假响应
                    if is_dry_run and not res:
                        res = {
                            "agent_id": "dry-run-agent-id", 
                            "name": package_info.name, 
                            "endpoint": "http://dry-run-endpoint", 
                            "api_key": "dry-run-key"
                        }
                    
                    # CreateAgentProduct 返回 order_id，需要轮询 list_agents 获取 agent_id
                    new_agent_id = res.get("agent_id")
                    order_id = res.get("order_id")
                    agent_name = res.get("name") or package_info.name
                    agent_endpoint = res.get("endpoint")
                    agent_api_key = res.get("api_key")

                    if order_id and not new_agent_id:
                        click.echo(f"   📋 订单已创建: {order_id}，等待实例创建...")
                        import time
                        for i in range(12):  # 最多等 60s
                            time.sleep(5)
                            try:
                                detail = await client.get_agent(name=package_info.name, include_api_key=True)
                                qa = detail.get("quick_access", {})
                                basic = detail.get("basic", {})
                                new_agent_id = basic.get("agent_id")
                                agent_name = basic.get("name") or agent_name
                                agent_endpoint = qa.get("public_endpoint")
                                agent_api_key = qa.get("api_key")
                                if new_agent_id:
                                    click.secho(f"   ✅ 实例已创建: {new_agent_id}", fg="green")
                                    break
                            except Exception:
                                pass
                            click.echo(f"   ⏳ 等待中... ({(i+1)*5}s)")

                        if not new_agent_id:
                            click.secho("   ⚠️  实例仍在创建中，稍后使用 'agentengine status' 查看", fg="yellow")

                    latest_access = {}
                    if new_agent_id and not is_dry_run:
                        latest_access = await get_latest_agent_access(
                            new_client,
                            agent_id=new_agent_id,
                            include_api_key=True,
                            retry_delays=(0.3, 0.7, 1.0),
                            on_error=lambda exc: logger.warning(
                                "Failed to refresh quick access for %s: %s",
                                new_agent_id,
                                exc,
                            ),
                        )
                        new_agent_id = latest_access.get("agent_id") or new_agent_id
                        agent_name = latest_access.get("name") or agent_name
                        agent_endpoint = latest_access.get("endpoint") or agent_endpoint
                        agent_api_key = latest_access.get("api_key") or agent_api_key
                    
                    # 保存 agent_id 到本地状态文件
                    self._save_state(state_file, {
                        "agent_id": new_agent_id,
                        "name": agent_name,
                        "region": target.region,
                        "endpoint": agent_endpoint,
                        "api_key": agent_api_key,  # 只在首次保存
                        "order_id": order_id,
                        "created_at": self._now_iso(),
                        **ui_state,
                    })
                    
                    click.echo(f"   💾 已保存状态到 .agentengine.state")
                    
                    return DeployResult(
                        status=DeployStatus.DEPLOYING, 
                        agent_id=new_agent_id,
                        agent_name=agent_name,
                        endpoint=agent_endpoint, 
                        api_key=agent_api_key,
                        message=f"✅ Agent ID: {new_agent_id or '(创建中)'} (首次部署, 订单: {order_id or '-'})"
                    )

        except DryRunExit as e:
            return DeployResult(
                status=DeployStatus.SKIPPED,
                message="✅ Dry Run Completed: 请求已打印，未执行实际变更。",
                metadata={"dry_run_request": e.payload or {}},
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
            async with AgentEngineClient(region=target.region, dry_run=dry_run) as client:
                res = await client.get_agent(agent_id)
                
                status_map = {
                    "running": DeployStatus.RUNNING,
                    "ready": DeployStatus.RUNNING,
                    "creating": DeployStatus.DEPLOYING,
                    "updating": DeployStatus.UPDATING,
                    "terminating": DeployStatus.STOPPING,
                    "scaling": DeployStatus.UPDATING,
                    "failed": DeployStatus.FAILED,
                    "error": DeployStatus.FAILED,
                    "unknown": DeployStatus.UNKNOWN
                }
                
                # 使状态匹配不区分大小写，以兼容服务端的改动 (Creating -> CREATING)
                current_status = (res.get("status") or "").lower()
                return DeployResult(
                    status=status_map.get(current_status, DeployStatus.UNKNOWN),
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
        project_dir = str(target.extra.get("project_dir") or "").strip()
        state_file = (Path(project_dir).resolve() if project_dir else Path(".").resolve()) / ".agentengine.state"

        try:
            async with AgentEngineClient(region=target.region, dry_run=dry_run) as client:
                click.echo(f"正在通过 Server 删除 Agent: {agent_id}...")
                success = await client.delete_agent(agent_id)
                if success and not dry_run and state_file.exists():
                    local_state = self._load_state(state_file)
                    if str(local_state.get("agent_id") or "").strip() == str(agent_id).strip():
                        try:
                            os.remove(state_file)
                            logger.info(f"Deleted local state file: {state_file}")
                        except Exception as cleanup_error:
                            logger.warning(f"Failed to delete local state file {state_file}: {cleanup_error}")
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
            async with AgentEngineClient(region=target.region, dry_run=dry_run) as client:
                res = await client.list_agents()
                
                results = []
                for agent in res.get("Agents", []):
                    current_status = (agent.get("status") or "").lower()
                    results.append(DeployResult(
                        status=DeployStatus.RUNNING if current_status in ("running", "ready") else (
                            DeployStatus.DEPLOYING if current_status == "creating" else DeployStatus.UNKNOWN
                        ),
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
            async with AgentEngineClient(region=target.region, dry_run=dry_run) as client:
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
