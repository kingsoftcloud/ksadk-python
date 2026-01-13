"""
Providers 包初始化

自动注册所有 Provider
"""

# 导入所有 Provider 以触发注册
from ksadk.deployment.providers.docker import DockerProvider
from ksadk.deployment.providers.k8s import K8sProvider

# 尝试导入可选 Provider
try:
    from ksadk.deployment.providers.serverless import ServerlessProvider
except ImportError:
    pass

__all__ = ["DockerProvider", "K8sProvider"]
