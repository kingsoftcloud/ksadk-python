"""
部署 Provider 注册表

使用装饰器模式自动注册 Provider，支持动态扩展。
"""

from typing import Dict, Type, List, Optional
from ksadk.deployment.base import BaseDeployProvider


class DeployProviderRegistry:
    """Provider 注册表
    
    使用方式:
        @DeployProviderRegistry.register("my-provider")
        class MyProvider(BaseDeployProvider):
            ...
    """
    
    _providers: Dict[str, Type[BaseDeployProvider]] = {}
    _instances: Dict[str, BaseDeployProvider] = {}
    
    @classmethod
    def register(cls, name: str):
        """装饰器: 注册 Provider
        
        Args:
            name: Provider 名称 (如 "serverless", "k8s", "docker")
        """
        def decorator(provider_class: Type[BaseDeployProvider]):
            if not issubclass(provider_class, BaseDeployProvider):
                raise TypeError(f"{provider_class} must inherit from BaseDeployProvider")
            
            provider_class.name = name
            cls._providers[name] = provider_class
            return provider_class
        
        return decorator
    
    @classmethod
    def get(cls, name: str, config: Dict = None) -> BaseDeployProvider:
        """获取 Provider 实例
        
        Args:
            name: Provider 名称
            config: Provider 配置
            
        Returns:
            Provider 实例
        """
        if name not in cls._providers:
            available = ", ".join(cls._providers.keys())
            raise ValueError(f"Unknown provider: {name}. Available: {available}")
        
        # 缓存实例 (相同 name 返回相同实例)
        cache_key = f"{name}:{hash(str(config))}"
        if cache_key not in cls._instances:
            cls._instances[cache_key] = cls._providers[name](config)
        
        return cls._instances[cache_key]
    
    @classmethod
    def get_class(cls, name: str) -> Type[BaseDeployProvider]:
        """获取 Provider 类 (不实例化)
        
        Args:
            name: Provider 名称
            
        Returns:
            Provider 类
        """
        if name not in cls._providers:
            raise ValueError(f"Unknown provider: {name}")
        return cls._providers[name]
    
    @classmethod
    def list_providers(cls) -> List[str]:
        """列出所有已注册的 Provider 名称
        
        Returns:
            Provider 名称列表
        """
        return list(cls._providers.keys())
    
    @classmethod
    def get_provider_info(cls) -> List[Dict]:
        """获取所有 Provider 的详细信息
        
        Returns:
            Provider 信息列表
        """
        result = []
        for name, provider_class in cls._providers.items():
            result.append({
                "name": name,
                "display_name": getattr(provider_class, "display_name", name),
                "description": getattr(provider_class, "description", ""),
                "supports_streaming": getattr(provider_class, "supports_streaming", False),
                "supports_scaling": getattr(provider_class, "supports_scaling", False),
                "requires_image_registry": getattr(provider_class, "requires_image_registry", False),
            })
        return result
    
    @classmethod
    def has_provider(cls, name: str) -> bool:
        """检查 Provider 是否存在
        
        Args:
            name: Provider 名称
            
        Returns:
            是否存在
        """
        return name in cls._providers
    
    @classmethod
    def clear(cls):
        """清空注册表 (主要用于测试)"""
        cls._providers.clear()
        cls._instances.clear()
