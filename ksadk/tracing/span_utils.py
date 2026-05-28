"""
Tracing 工具函数

提供公共的 Trace 元数据准备函数，供所有 Runner 使用。
"""

from pathlib import Path
from typing import Any, List, Optional, Tuple


def prepare_trace_metadata(
    detection_result: Any = None,
) -> Tuple[Optional[str], List[str], Optional[str], Optional[str]]:
    """准备 Trace 元数据 (Tags, UserID, etc.)

    Args:
        detection_result: 框架检测结果，用于获取 Agent 名称

    Returns:
        Tuple of (user_id, tags, version, agent_name)
    """
    user_id = None
    tags: List[str] = []
    version = None
    agent_name = None

    try:
        from ksadk.configs import settings

        agent_config = settings.agent

        user_id = agent_config.user_id
        version = agent_config.version
        tags = list(agent_config.tags or [])

        # Add Environment
        if agent_config.environment and agent_config.environment not in tags:
            tags.append(agent_config.environment)

        # Add Region (Kingsoft Cloud)
        if settings.cloud.region and settings.cloud.region not in tags:
            tags.append(settings.cloud.region)

        # Add Model Name
        if settings.model.model_name and settings.model.model_name not in tags:
            tags.append(settings.model.model_name)

        # Add Agent Name (Configured -> Fallback)
        agent_name = agent_config.agent_name
        if not agent_name and detection_result:
            try:
                # Fallback to package name
                agent_name = Path(detection_result.package_path).name
            except Exception:
                pass

        if agent_name and agent_name not in tags:
            tags.append(agent_name)

        # Add Agent ID
        if agent_config.agent_id and agent_config.agent_id not in tags:
            tags.append(agent_config.agent_id)

        # Add Tenant ID (Account ID)
        if agent_config.tenant_id and agent_config.tenant_id not in tags:
            tags.append(agent_config.tenant_id)

    except ImportError:
        pass
    except Exception:
        pass

    return user_id, tags, version, agent_name


def get_langfuse_callback(
    session_id: str,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    version: Optional[str] = None,
    trace_name: Optional[str] = None,
) -> Optional[Any]:
    """获取 Langfuse Callback Handler

    Args:
        session_id: 会话 ID
        user_id: 用户 ID
        tags: 标签列表
        version: 版本号
        trace_name: Trace 名称

    Returns:
        Langfuse callback handler 或 None
    """
    try:
        from ksadk.tracing import get_langfuse_callback_handler

        handler = get_langfuse_callback_handler()
        if handler:
            # 设置 session 和 metadata
            handler.session_id = session_id
            if user_id:
                handler.user_id = user_id
            if tags:
                handler.tags = tags
            if version:
                handler.version = version
            if trace_name:
                handler.trace_name = trace_name
        return handler
    except ImportError:
        return None
    except Exception:
        return None
