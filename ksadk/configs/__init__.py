"""
AgentEngine 统一配置管理

所有配置从环境变量读取，优先使用开源/通用标准，兼容 Serverless 平台定制变量。

使用方式:
    from ksadk.configs import settings
    
环境变量优先级 (举例):
    Model:    OPENAI_API_KEY > LLM_API_KEY > MODEL_API_KEY
    Langfuse: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST
    Agent:    AGENT_ID > AGENT_RUNTIME_ID

默认值:
    OPENAI_API_BASE: 金山云模型服务公开地址
    MODEL_NAME: glm-5.1
"""

from ksadk.configs.settings import (
    # 全局配置入口
    settings,
    Settings,
    # 各配置类
    ModelConfig,
    LangfuseConfig,
    AgentConfig,
    KingsoftCloudConfig,
    CodeModeConfig,
    OTelConfig,
    # KSPMAS 服务
    get_kspmas_api_base,
    KSPMAS_PUBLIC_URL,
    DEFAULT_MODEL_NAME,
    setup_environment,
)

__all__ = [
    # 主入口
    "settings",
    "Settings",
    # 配置类
    "ModelConfig",
    "LangfuseConfig",
    "AgentConfig",
    "KingsoftCloudConfig",
    "CodeModeConfig",
    "OTelConfig",
    # KSPMAS 服务
    "get_kspmas_api_base",
    "KSPMAS_PUBLIC_URL",
    "DEFAULT_MODEL_NAME",
    "setup_environment",
]
