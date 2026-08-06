"""
Langfuse 集成工具

提供 LangChain/LangGraph Runner 共用的 Langfuse 集成功能
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 全局缓存
_langfuse_callback = None
_cloud_monitor_langfuse_callback = None


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_flag_disabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"0", "false", "no", "off", "disabled"}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _create_langfuse_callback(
    *,
    label: str,
    public_key: str,
    secret_key: str = "",
    host: str = "",
) -> Any | None:
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        resource_attributes = {}
        service_name = os.getenv("OTEL_SERVICE_NAME", "").strip()
        if service_name:
            resource_attributes["service.name"] = service_name
        tracer_provider = TracerProvider(resource=Resource.create(resource_attributes))
        Langfuse(
            public_key=public_key,
            secret_key=secret_key or None,
            base_url=host or None,
            tracer_provider=tracer_provider,
        )
        try:
            handler = CallbackHandler(public_key=public_key)
        except TypeError:
            handler = CallbackHandler()
        logger.info(
            "%s Langfuse CallbackHandler initialized "
            "(host: %s public_key_present=%s secret_key_present=%s isolated_provider=True)",
            label,
            host or "default",
            bool(public_key),
            bool(secret_key),
        )
        return handler
    except ImportError as e:
        logger.warning("Langfuse not installed for %s callback: %s", label, e)
        return None
    except Exception as e:
        logger.error("Failed to create %s Langfuse CallbackHandler: %s", label, e)
        return None


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
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    if not public_key:
        return None

    _langfuse_callback = _create_langfuse_callback(
        label="Primary",
        public_key=public_key,
        secret_key=os.getenv("LANGFUSE_SECRET_KEY", "").strip(),
        host=_first_env("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )
    return _langfuse_callback


def get_cloud_monitor_langfuse_callback():
    """获取云监控 Langfuse SDK CallbackHandler。"""
    global _cloud_monitor_langfuse_callback

    if not _env_flag_enabled("LANGFUSE_USE_CALLBACK"):
        return None
    if _env_flag_disabled("CLOUD_MONITOR_LANGFUSE_ENABLED"):
        logger.info(
            "CloudMonitor Langfuse CallbackHandler disabled by CLOUD_MONITOR_LANGFUSE_ENABLED"
        )
        return None
    if _cloud_monitor_langfuse_callback is not None:
        return _cloud_monitor_langfuse_callback

    public_key = os.getenv("CLOUD_MONITOR_LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("CLOUD_MONITOR_LANGFUSE_SECRET_KEY", "").strip()
    host = _first_env("CLOUD_MONITOR_LANGFUSE_HOST", "CLOUD_MONITOR_OTLP_ENDPOINT")
    if not public_key and not secret_key and not host:
        return None
    if not public_key or not secret_key or not host:
        logger.warning(
            "CloudMonitor Langfuse CallbackHandler skipped: public_key_present=%s "
            "secret_key_present=%s host_present=%s",
            bool(public_key),
            bool(secret_key),
            bool(host),
        )
        return None

    _cloud_monitor_langfuse_callback = _create_langfuse_callback(
        label="CloudMonitor",
        public_key=public_key,
        secret_key=secret_key,
        host=host,
    )
    return _cloud_monitor_langfuse_callback


def get_langfuse_callbacks() -> list[Any]:
    """获取所有 Langfuse CallbackHandler，原平台和云监控各自独立。"""
    callbacks = []
    primary = get_langfuse_callback()
    if primary:
        callbacks.append(primary)

    cloud_monitor = get_cloud_monitor_langfuse_callback()
    if cloud_monitor:
        callbacks.append(cloud_monitor)

    return callbacks


def get_langfuse_metadata(session_id: str | None = None) -> dict[str, Any]:
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
    metadata: dict[str, Any] = {}

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


def prepare_trace_metadata(session_id: str | None = None) -> tuple[Any, list[Any], Any, Any]:
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
