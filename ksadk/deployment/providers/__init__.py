"""
Providers 包初始化

自动注册所有 Provider
"""

# 导入 Provider 以触发注册
from ksadk.deployment.providers.serverless import ServerlessProvider

__all__ = ["ServerlessProvider"]
