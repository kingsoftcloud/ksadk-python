"""
部署模块

支持多种部署目标:
- docker: 本地 Docker 容器
- k8s: Kubernetes 集群
- serverless: 金山云 Serverless 计算引擎
"""

from ksadk.deployment.base import (
    BaseDeployProvider,
    DeployTarget,
    DeployResult,
    DeployStatus,
    PackageInfo,
    ResourceSpec,
    ScalingConfig,
    NetworkConfig,
)
from ksadk.deployment.registry import DeployProviderRegistry

# 导入并注册所有 Provider
from ksadk.deployment import providers


class DeploymentManager:
    """部署管理器 - 统一入口
    
    使用方式:
        manager = DeploymentManager()
        provider = manager.get_provider("docker")
        result = await provider.deploy(package_info, target)
    """
    
    @staticmethod
    def get_provider(name: str, config: dict = None) -> BaseDeployProvider:
        """获取部署 Provider
        
        Args:
            name: Provider 名称 (docker, k8s, serverless)
            config: Provider 配置
            
        Returns:
            Provider 实例
        """
        return DeployProviderRegistry.get(name, config)
    
    @staticmethod
    def list_providers() -> list:
        """列出所有可用 Provider"""
        return DeployProviderRegistry.get_provider_info()
    
    @staticmethod
    def has_provider(name: str) -> bool:
        """检查 Provider 是否存在"""
        return DeployProviderRegistry.has_provider(name)
    
    # 向后兼容: create() 方法
    @classmethod
    def create(cls, target: str) -> DeployTarget:
        """创建部署目标
        
        Args:
            target: 部署目标 (docker, k8s, serverless)
            
        Returns:
            部署目标实例
        """
        return cls.get_provider(target)


__all__ = [
    "DeploymentManager",
    "BaseDeployProvider",
    "DeployTarget",
    "DeployResult",
    "DeployStatus",
    "PackageInfo",
    "ResourceSpec",
    "ScalingConfig",
    "NetworkConfig",
    "DeployProviderRegistry",
]
