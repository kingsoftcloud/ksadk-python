"""
AgentEngine 统一配置管理

从环境变量加载配置。
优先使用开源/业界标准变量名，同时兼容 Serverless 平台定制变量。

使用方式:
    from ksadk.configs import settings
    
    # 访问模型配置
    print(settings.model.api_key)
    print(settings.model.api_base)
    
    # 访问 Langfuse 配置
    print(settings.langfuse.public_key)
    
    # 访问 Agent 运行时信息
    print(settings.agent.agent_id)
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _get_env(*keys: str, default: str = None) -> Optional[str]:
    """从多个环境变量 key 中获取第一个存在的值
    
    按优先级顺序匹配，支持别名兼容。
    """
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return default


# =============================================================================
# 金山云模型服务 (KSPMAS)
# =============================================================================

# 服务地址
KSPMAS_PUBLIC_URL = "https://kspmas.ksyun.com/v1"

# 默认模型
DEFAULT_MODEL_NAME = "glm-5.1"


def optimize_kspmas_url(url: str) -> str:
    """优化 KSPMAS URL
    
    公开 SDK 默认保留用户配置的公开地址。托管环境如需使用专有内网地址，
    应通过运行时环境变量显式注入，不在开源代码中写死内部 endpoint。
    """
    return url


def get_kspmas_api_base() -> str:
    """获取 KSPMAS 模型服务的 API Base URL
    
    默认使用公开地址；如需专有网络 endpoint，请设置 OPENAI_BASE_URL /
    OPENAI_API_BASE / LLM_API_BASE / MODEL_API_BASE。
    """
    return KSPMAS_PUBLIC_URL


# =============================================================================
# 模型配置 (LLM)
# =============================================================================

@dataclass
class ModelConfig:
    """模型配置
    
    标准环境变量 (优先):
        OPENAI_API_KEY       - API Key (OpenAI 标准)
        OPENAI_BASE_URL      - API Base URL (OpenAI 标准)
        OPENAI_MODEL_NAME    - 模型名称
    
    通用格式:
        MODEL_NAME           - 模型名称
    
    Serverless 平台兼容:
        LLM_API_KEY          - 模型 API Key
        LLM_API_BASE         - 模型 API 地址
        LLM_MODEL            - 模型名称
    
    别名 (兼容):
        OPENAI_API_BASE      - API Base URL 别名
    
    默认值:
        OPENAI_BASE_URL: 金山云模型服务公开地址
        MODEL_NAME: glm-5.1
    """
    
    @property
    def api_key(self) -> Optional[str]:
        """获取模型 API Key"""
        return _get_env(
            "OPENAI_API_KEY",        # OpenAI 标准 (优先)
            "LLM_API_KEY",           # Serverless 平台兼容
            "MODEL_API_KEY",         # 通用
        )
    
    @property
    def api_base(self) -> str:
        """获取模型 API Base URL
        
        如果未配置，默认使用金山云模型服务公开地址。
        """
        configured = _get_env(
            "OPENAI_BASE_URL",       # OpenAI 标准 (优先)
            "OPENAI_API_BASE",       # OpenAI 别名 (兼容)
            "LLM_API_BASE",          # Serverless 平台兼容
            "MODEL_API_BASE",        # 通用
        )
        
        if configured:
            return optimize_kspmas_url(configured)
            
        return get_kspmas_api_base()
    
    @property
    def model_name(self) -> str:
        """获取模型名称
        
        如果未配置，默认使用 glm-5.1。
        """
        configured = _get_env(
            "OPENAI_MODEL_NAME",     # OpenAI 格式 (优先)
            "MODEL_NAME",            # 通用
            "LLM_MODEL",             # Serverless 平台兼容
        )
        return configured or DEFAULT_MODEL_NAME
    
    @property
    def is_configured(self) -> bool:
        """是否已配置 API Key"""
        return bool(self.api_key)
    
    @property
    def is_using_internal_network(self) -> bool:
        """是否使用内网地址"""
        return False
    
    def to_dict(self) -> dict:
        """转换为字典 (用于 LiteLLM 等)"""
        result = {}
        if self.api_key:
            result["api_key"] = self.api_key
        result["api_base"] = self.api_base
        result["model"] = self.model_name
        return result


# =============================================================================
# Langfuse 配置
# =============================================================================

@dataclass
class LangfuseConfig:
    """Langfuse 可观测性配置
    
    标准环境变量 (Langfuse 官方):
        LANGFUSE_PUBLIC_KEY   - API Public Key
        LANGFUSE_SECRET_KEY   - API Secret Key
        LANGFUSE_HOST         - Langfuse 服务地址
    
    Serverless 平台扩展:
        LANGFUSE_PROJECT_ID   - 项目 ID (区分用户)
        LANGCHAIN_TRACING_V2  - 启用 LangChain 追踪
    """
    
    @property
    def public_key(self) -> Optional[str]:
        return os.getenv("LANGFUSE_PUBLIC_KEY")
    
    @property
    def secret_key(self) -> Optional[str]:
        return os.getenv("LANGFUSE_SECRET_KEY")
    
    @property
    def host(self) -> str:
        return _get_env(
            "LANGFUSE_HOST",
            "LANGFUSE_BASE_URL",
            default="http://localhost:3000"
        )
    
    @property
    def project_id(self) -> Optional[str]:
        """Langfuse 项目 ID (Serverless 平台扩展)"""
        return os.getenv("LANGFUSE_PROJECT_ID")
    
    @property
    def tracing_enabled(self) -> bool:
        """是否启用 LangChain tracing"""
        return os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"
    
    @property
    def is_enabled(self) -> bool:
        """是否启用 Langfuse (有 public_key 就启用)"""
        return bool(self.public_key)
    
    @property
    def is_configured(self) -> bool:
        """是否完整配置"""
        return bool(self.public_key and self.secret_key)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "public_key": self.public_key,
            "secret_key": self.secret_key,
            "host": self.host,
        }


# =============================================================================
# Agent 配置
# =============================================================================

@dataclass
class AgentConfig:
    """Agent 配置
    
    标准环境变量 (OpenTelemetry 兼容):
        OTEL_SERVICE_NAME       - 服务名称 (映射到 agent_name)
        OTEL_RESOURCE_ATTRIBUTES - 资源属性 (key=value 格式)
    
    通用环境变量:
        AGENT_ID          - Agent 唯一 ID
        AGENT_NAME        - Agent 名称
        TENANT_ID         - 租户 ID
        USER_ID           - 用户 ID
        ENVIRONMENT       - 运行环境 (dev/staging/prod)
        VERSION           - 版本号
    
    Serverless 平台兼容:
        AGENT_RUNTIME_ID   - Agent 唯一 ID
        AGENT_RUNTIME_NAME - Agent 名称
        ACCOUNT_ID         - 账户 ID (租户)
    """
    
    @property
    def agent_id(self) -> Optional[str]:
        """Agent 唯一 ID"""
        return _get_env(
            "AGENT_ID",              # 通用 (优先)
            "AGENT_RUNTIME_ID",      # Serverless 平台兼容
        )
    
    @property
    def agent_name(self) -> Optional[str]:
        """Agent 名称"""
        return _get_env(
            "AGENT_NAME",            # 通用 (优先)
            "AGENT_RUNTIME_NAME",    # Serverless 平台兼容
            "OTEL_SERVICE_NAME",     # OpenTelemetry 标准
        )
    
    @property
    def tenant_id(self) -> Optional[str]:
        """租户 ID"""
        return _get_env(
            "TENANT_ID",             # 通用 (优先)
            "ACCOUNT_ID",            # Serverless 平台兼容
        )
    
    @property
    def user_id(self) -> Optional[str]:
        """用户 ID"""
        return os.getenv("USER_ID")
    
    @property
    def session_id(self) -> Optional[str]:
        """会话 ID (用于 Langfuse Sessions 功能)"""
        return os.getenv("SESSION_ID")
    
    @property
    def environment(self) -> Optional[str]:
        """运行环境 (dev/staging/prod)"""
        return os.getenv("ENVIRONMENT")
    
    @property
    def version(self) -> Optional[str]:
        """Agent 版本"""
        return os.getenv("VERSION")
    
    @property
    def tags(self) -> Optional[List[str]]:
        """标签列表 (逗号分隔)"""
        tags_str = os.getenv("TAGS", "")
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        return tags if tags else None
    
    @property
    def extra_metadata(self) -> Optional[Dict[str, Any]]:
        """额外的元数据"""
        extra = {}
        
        # 1. 解析 JSON 格式的 metadata
        extra_json = os.getenv("METADATA")
        if extra_json:
            try:
                extra.update(json.loads(extra_json))
            except json.JSONDecodeError:
                logger.warning(f"Invalid METADATA JSON: {extra_json}")
        
        # 2. 解析 OTEL_RESOURCE_ATTRIBUTES
        otel_attrs = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
        if otel_attrs:
            for pair in otel_attrs.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    if key not in ("service.name", "service.version"):
                        extra[key] = value
        
        return extra if extra else None
    
    @property
    def is_configured(self) -> bool:
        """是否有 Agent 信息"""
        return bool(self.agent_id or self.agent_name)
    
    # ------------------
    # Langfuse 集成方法
    # ------------------
    
    def to_langfuse_params(self) -> dict:
        """转换为 Langfuse trace() 方法的原生参数"""
        params = {}
        if self.session_id:
            params["session_id"] = self.session_id
        if self.user_id:
            params["user_id"] = self.user_id
        if self.tags:
            params["tags"] = self.tags
        if self.version:
            params["version"] = self.version
        return params
    
    def to_langfuse_metadata(self) -> dict:
        """转换为 Langfuse metadata 字段"""
        metadata = {}
        if self.agent_id:
            metadata["agent_id"] = self.agent_id
        if self.agent_name:
            metadata["agent_name"] = self.agent_name
        if self.tenant_id:
            metadata["tenant_id"] = self.tenant_id
        if self.environment:
            metadata["environment"] = self.environment
        if self.extra_metadata:
            metadata.update(self.extra_metadata)
        return metadata


# =============================================================================
# 金山云配置
# =============================================================================

@dataclass
class KingsoftCloudConfig:
    """金山云 SDK 配置
    
    环境变量:
        KS_ACCESS_KEY_ID     - Access Key ID
        KS_SECRET_ACCESS_KEY - Secret Access Key
        KS_REGION            - 区域 (默认 cn-beijing-6)
    """
    
    @property
    def access_key_id(self) -> Optional[str]:
        return os.getenv("KS_ACCESS_KEY_ID")
    
    @property
    def secret_access_key(self) -> Optional[str]:
        return os.getenv("KS_SECRET_ACCESS_KEY")
    
    @property
    def region(self) -> str:
        return os.getenv("KS_REGION", "cn-beijing-6")
    
    @property
    def is_configured(self) -> bool:
        """是否已配置"""
        return bool(self.access_key_id and self.secret_access_key)


# =============================================================================
# Code 模式配置
# =============================================================================

@dataclass
class CodeModeConfig:
    """Code 模式配置 (Serverless 平台)
    
    环境变量:
        PYTHONPATH - Python 模块路径
        CODE_PATH  - 代码目录
    """
    
    @property
    def python_path(self) -> Optional[str]:
        return os.getenv("PYTHONPATH")
    
    @property
    def code_path(self) -> Optional[str]:
        return os.getenv("CODE_PATH")
    
    @property
    def is_code_mode(self) -> bool:
        """是否为 Code 模式"""
        return bool(self.code_path)


# =============================================================================
# OpenTelemetry 配置
# =============================================================================

@dataclass
class OTelConfig:
    """OpenTelemetry 配置 (标准环境变量)
    
    环境变量:
        OTEL_SERVICE_NAME           - 服务名称
        OTEL_RESOURCE_ATTRIBUTES    - 资源属性 (key=value,key2=value2)
        OTEL_EXPORTER_OTLP_ENDPOINT - OTLP 导出端点
    """
    
    @property
    def service_name(self) -> Optional[str]:
        return os.getenv("OTEL_SERVICE_NAME")
    
    @property
    def resource_attributes(self) -> Optional[Dict[str, str]]:
        """解析 OTEL_RESOURCE_ATTRIBUTES"""
        attrs_str = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
        if not attrs_str:
            return None
        
        attrs = {}
        for pair in attrs_str.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                attrs[key] = value
        return attrs if attrs else None
    
    @property
    def otlp_endpoint(self) -> Optional[str]:
        return _get_env(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        )
    
    @property
    def is_enabled(self) -> bool:
        """是否启用 OTLP 导出"""
        return bool(self.otlp_endpoint)


# =============================================================================
# 统一配置入口
# =============================================================================

@dataclass
class Settings:
    """统一配置入口
    
    使用方式:
        from ksadk.configs import settings
        
        # 模型配置
        if settings.model.is_configured:
            llm = LiteLLM(api_key=settings.model.api_key)
        
        # Langfuse 配置
        if settings.langfuse.is_enabled:
            setup_tracing(langfuse_config=settings.langfuse.to_dict())
        
        # Agent 信息
        print(settings.agent.agent_id)
    """
    model: ModelConfig = field(default_factory=ModelConfig)
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    cloud: KingsoftCloudConfig = field(default_factory=KingsoftCloudConfig)
    code: CodeModeConfig = field(default_factory=CodeModeConfig)
    otel: OTelConfig = field(default_factory=OTelConfig)
    
    def summary(self) -> str:
        """输出配置摘要"""
        lines = ["AgentEngine Configuration:"]
        lines.append(f"  Model:    {'✅ Configured' if self.model.is_configured else '❌ Not configured'}")
        lines.append(f"  Langfuse: {'✅ Enabled' if self.langfuse.is_enabled else '⚪ Disabled'}")
        
        agent_info = self.agent.agent_id or self.agent.agent_name
        lines.append(f"  Agent:    {'✅ ' + agent_info if agent_info else '⚪ No info'}")
        
        lines.append(f"  Cloud:    {'✅ Configured' if self.cloud.is_configured else '⚪ Not configured'}")
        lines.append(f"  CodeMode: {'✅ ' + self.code.code_path if self.code.is_code_mode else '⚪ Container mode'}")
        lines.append(f"  OTEL:     {'✅ Enabled' if self.otel.is_enabled else '⚪ Disabled'}")
        return "\n".join(lines)
    
    def __repr__(self) -> str:
        return self.summary()


# 全局单例
settings = Settings()


# ------------------
# 环境初始化工具
# ------------------

def setup_environment(agent_path: "Path"):
    """加载环境变量并注入智能默认配置
    
    1. 加载项目根目录的 .env 文件 (override=True)
    2. 注入 OPENAI_API_BASE (智能检测)
    3. 注入 OPENAI_MODEL_NAME (默认值)
    """
    import click
    import os
    from pathlib import Path
    try:
        from dotenv import load_dotenv
    except ImportError:
        # 如果没有安装 python-dotenv，则跳过 loading .env
        load_dotenv = None
        
    # 1. 加载 .env
    if load_dotenv:
        # Ensure agent_path is a Path object
        if isinstance(agent_path, str):
            agent_path = Path(agent_path)
            
        env_file = agent_path / ".env"
        if env_file.exists():
            # override=False: 保留通过 API/Serverless 平台注入的环境变量 (优先级高)
            load_dotenv(env_file, override=False)

    # 2. Coze SDK 兼容映射
    # 某些 Coze 导出项目（tool 内使用 coze_coding_dev_sdk）会强依赖 COZE_* 环境变量，
    # 本地通常只配置了 OPENAI_*，这里做一次非覆盖式映射。
    coze_api_key = os.getenv("COZE_WORKLOAD_IDENTITY_API_KEY")
    coze_base_url = os.getenv("COZE_INTEGRATION_BASE_URL")
    coze_model_base_url = os.getenv("COZE_INTEGRATION_MODEL_BASE_URL")

    openai_api_key = _get_env("OPENAI_API_KEY", "LLM_API_KEY", "MODEL_API_KEY")
    openai_base_url = _get_env(
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "LLM_API_BASE",
        "MODEL_API_BASE",
    )

    if not coze_api_key and openai_api_key:
        os.environ["COZE_WORKLOAD_IDENTITY_API_KEY"] = openai_api_key
    if not coze_base_url and openai_base_url:
        os.environ["COZE_INTEGRATION_BASE_URL"] = openai_base_url
    if not coze_model_base_url and openai_base_url:
        os.environ["COZE_INTEGRATION_MODEL_BASE_URL"] = openai_base_url

    # 3. 注入 Intelligent Defaults
    # API Base. 兼容 OpenAI SDK/LangChain 常见的 OPENAI_BASE_URL 与历史 OPENAI_API_BASE。
    if not os.getenv("OPENAI_BASE_URL") or not os.getenv("OPENAI_API_BASE"):
        api_base = settings.model.api_base
        if api_base:
            if not os.getenv("OPENAI_BASE_URL"):
                os.environ["OPENAI_BASE_URL"] = api_base
            if not os.getenv("OPENAI_API_BASE"):
                os.environ["OPENAI_API_BASE"] = api_base
            click.echo(f"🔧 API Base: {click.style(api_base, fg='cyan')} (Auto-detected)")
            
    # Model Name
    if not os.getenv("OPENAI_MODEL_NAME"):
        model_name = settings.model.model_name
        os.environ["OPENAI_MODEL_NAME"] = model_name
        click.echo(f"🧠 Model:    {click.style(model_name, fg='cyan')} (Default)")
    if not os.getenv("MODEL_NAME") and os.getenv("OPENAI_MODEL_NAME"):
        os.environ["MODEL_NAME"] = os.getenv("OPENAI_MODEL_NAME", "")
