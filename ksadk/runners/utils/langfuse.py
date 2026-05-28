"""
Langfuse 集成工具

提供 LangChain/LangGraph Runner 共用的 Langfuse 集成功能
"""

import os
import logging

logger = logging.getLogger(__name__)

# 全局缓存
_langfuse_callback = None


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def get_langfuse_callback():
    """获取 Langfuse CallbackHandler
    
    Returns:
        CallbackHandler 实例，未配置时返回 None
    """
    global _langfuse_callback

    if not _env_flag_enabled("LANGFUSE_USE_CALLBACK"):
        logger.debug("Langfuse CallbackHandler disabled; using OTLP direct exporter by default")
        return None
    
    if _langfuse_callback is not None:
        return _langfuse_callback
    
    # 检查是否配置了 Langfuse
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    if not public_key:
        return None
    
    try:
        from langfuse.langchain import CallbackHandler
        
        handler = CallbackHandler()
        _langfuse_callback = handler
        
        logger.info(f"Langfuse CallbackHandler initialized (host: {os.getenv('LANGFUSE_BASE_URL', 'default')})")
        return handler
        
    except ImportError as e:
        logger.warning(f"Langfuse not installed: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to create Langfuse CallbackHandler: {e}")
        return None


def get_langfuse_metadata(session_id: str = None) -> dict:
    """获取 Langfuse 的 metadata 字典
    
    通过 metadata 字段传递 trace 属性:
    - langfuse_user_id
    - langfuse_session_id
    - langfuse_tags
    
    Args:
        session_id: 会话 ID (可选)
    
    Returns:
        包含 Langfuse 属性的 metadata 字典
    """
    metadata = {}
    
    if session_id:
        metadata["langfuse_session_id"] = session_id
    
    try:
        from ksadk.configs import settings
        
        agent_config = settings.agent
        
        if agent_config.user_id:
            metadata["langfuse_user_id"] = agent_config.user_id
        
        if not session_id and agent_config.session_id:
            metadata["langfuse_session_id"] = agent_config.session_id
        
        tags = list(agent_config.tags or [])
        if agent_config.environment and agent_config.environment not in tags:
            tags.append(agent_config.environment)
        if agent_config.agent_name and agent_config.agent_name not in tags:
            tags.append(agent_config.agent_name)
        if tags:
            metadata["langfuse_tags"] = tags
            
    except (ImportError, Exception):
        pass
    
    return metadata


def prepare_trace_metadata(session_id: str = None) -> tuple:
    """准备 Trace 元数据
    
    Returns:
        (user_id, tags, version, agent_name) 元组
    """
    user_id = None
    tags = []
    version = None
    agent_name = None
    
    try:
        from ksadk.configs import settings
        
        agent_config = settings.agent
        user_id = agent_config.user_id
        tags = list(agent_config.tags or [])
        version = agent_config.version
        agent_name = agent_config.agent_name
        
        if agent_config.environment and agent_config.environment not in tags:
            tags.append(agent_config.environment)
            
    except (ImportError, Exception):
        pass
    
    return user_id, tags, version, agent_name
