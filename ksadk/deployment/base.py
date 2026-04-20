"""
部署 Provider 基类与数据模型
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from enum import Enum


class DeployStatus(str, Enum):
    """部署状态"""
    PENDING = "pending"
    PACKAGING = "packaging"
    BUILDING = "building"
    PUSHING = "pushing"
    DEPLOYING = "deploying"
    RUNNING = "running"
    UPDATING = "updating"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ResourceSpec(BaseModel):
    """资源规格"""
    cpu: str = "2"
    memory: str = "4Gi"


class ScalingConfig(BaseModel):
    """扩缩容配置"""
    min_replicas: int = 1
    max_replicas: int = 10
    concurrency: int = 10


class NetworkConfig(BaseModel):
    """网络配置"""
    access_type: str = "public"  # public | private
    enable_https: bool = True
    enable_public_access: bool = False
    enable_vpc_access: bool = False
    vpc_id: str = ""
    subnet_id: str = ""
    security_group_id: str = ""
    availability_zone: str = ""


class StorageSpec(BaseModel):
    """持久化存储配置。"""

    mount_path: str = ""
    size_gi: Optional[int] = None


class DeployTarget(BaseModel):
    """部署目标配置"""
    provider: str                      # serverless | faas | k8s | docker
    region: str = "cn-north-1"
    project_id: str = "default"
    
    # 通用配置
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    scaling: ScalingConfig = Field(default_factory=ScalingConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageSpec = Field(default_factory=StorageSpec)

    # Provider 特定配置
    extra: Dict[str, Any] = Field(default_factory=dict)


class DeployResult(BaseModel):
    """部署结果"""
    status: DeployStatus = DeployStatus.UNKNOWN
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    message: str = ""
    
    # 额外元数据
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    def is_success(self) -> bool:
        return self.status in (DeployStatus.RUNNING, DeployStatus.DEPLOYING)


class PackageInfo(BaseModel):
    """打包信息"""
    name: str
    framework: str
    build_dir: str
    project_dir: str = ""  # 原始项目目录 (用于读取/写入本地状态)
    dockerfile: Optional[str] = None
    image: Optional[str] = None
    entry_point: str = "agent.py"
    
    # 额外信息
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BaseDeployProvider(ABC):
    """部署 Provider 基类
    
    所有部署 Provider 必须继承此类并实现抽象方法。
    """
    
    # Provider 标识
    name: str = "base"
    display_name: str = "Base Provider"
    description: str = ""
    
    # 能力标识
    supports_streaming: bool = False
    supports_scaling: bool = False
    requires_image_registry: bool = False
    
    def __init__(self, config: Dict[str, Any] = None):
        """初始化 Provider
        
        Args:
            config: Provider 级别配置 (如 API Key、区域默认值等)
        """
        self.config = config or {}
    
    @abstractmethod
    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证部署配置
        
        Args:
            target: 部署目标配置
            
        Returns:
            (is_valid, error_message)
        """
        pass
    
    @abstractmethod
    async def package(self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None) -> PackageInfo:
        """打包项目
        
        Args:
            project_dir: 项目目录
            detection_result: 框架检测结果
            config: 项目配置 (agentengine.yaml)
            
        Returns:
            打包信息
        """
        pass
    
    @abstractmethod
    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建镜像/代码包
        
        Args:
            package_info: 打包信息
            target: 部署目标
            
        Returns:
            更新后的打包信息 (含镜像地址等)
        """
        pass
    
    @abstractmethod
    async def deploy(self, package_info: PackageInfo, target: DeployTarget) -> DeployResult:
        """部署
        
        Args:
            package_info: 打包信息
            target: 部署目标
            
        Returns:
            部署结果
        """
        pass
    
    @abstractmethod
    async def get_status(self, agent_id: str, target: DeployTarget) -> DeployResult:
        """获取部署状态
        
        Args:
            agent_id: Agent ID
            target: 部署目标
            
        Returns:
            部署状态
        """
        pass
    
    @abstractmethod
    async def destroy(self, agent_id: str, target: DeployTarget) -> bool:
        """销毁部署
        
        Args:
            agent_id: Agent ID
            target: 部署目标
            
        Returns:
            是否成功
        """
        pass
    
    async def list_agents(self, target: DeployTarget) -> List[DeployResult]:
        """列出所有 Agent (可选实现)
        
        Args:
            target: 部署目标
            
        Returns:
            Agent 列表
        """
        raise NotImplementedError(f"{self.name} does not support list_agents")
    
    async def invoke(self, agent_id: str, message: str, target: DeployTarget) -> str:
        """调用 Agent (可选实现)
        
        Args:
            agent_id: Agent ID
            message: 输入消息
            target: 部署目标
            
        Returns:
            Agent 响应
        """
        raise NotImplementedError(f"{self.name} does not support invoke")
