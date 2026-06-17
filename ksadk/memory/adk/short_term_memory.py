"""ShortTermMemory - 短期记忆 (会话管理)

封装 Google ADK 的 SessionService，提供统一的会话创建和管理接口。
通过环境变量配置后端类型，由 ADKRunner 自动注入。

参考 VeADK: veadk/memory/short_term_memory.py

环境变量:
    KSADK_SESSION_BACKEND: 统一 session 后端类型 (memory / local / sqlite / postgres)
    KSADK_SESSION_DSN: 统一 session 数据库 DSN
    KSADK_STM_BACKEND: 旧短期记忆后端类型 (local / sqlite / database)
    KSADK_STM_DB_URL: 旧数据库连接 URL (sqlite/database 时需要)

使用示例:
    # InMemory (默认，开发测试)
    stm = ShortTermMemory(backend="local")

    # SQLite 持久化
    stm = ShortTermMemory(backend="sqlite")

    # 自定义数据库
    stm = ShortTermMemory(db_url="postgresql+asyncpg://user:pass@host/db")
"""

import logging
import os
from typing import Any, Literal, Optional

from google.adk.sessions import (
    BaseSessionService,
    InMemorySessionService,
    Session,
)
from pydantic import BaseModel, PrivateAttr

logger = logging.getLogger(__name__)


def _env_first(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    return ""


def _normalize_backend_name(backend: str) -> str:
    normalized = str(backend or "").strip().lower()
    if normalized == "memory":
        return "local"
    if normalized == "postgres":
        return "database"
    return normalized


def _normalize_database_url(db_url: str) -> str:
    """Normalize sqlite URLs for google.adk DatabaseSessionService.

    ADK uses SQLAlchemy async engines underneath, so plain `sqlite:///...`
    URLs fail while `sqlite+aiosqlite:///...` works.
    """
    normalized = str(db_url or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("sqlite+aiosqlite:"):
        return normalized
    if normalized.startswith("sqlite:"):
        return "sqlite+aiosqlite:" + normalized[len("sqlite:") :]
    return normalized


def _session_backend_requires_database_url(backend: str) -> bool:
    return _normalize_backend_name(backend) == "database"


class ShortTermMemory(BaseModel):
    """短期记忆 - 会话管理

    封装 Google ADK 的 SessionService，所有短期记忆内容（系统提示、
    历史消息、模型回复）都会发送给模型。

    Attributes:
        backend: 后端类型
            - "local": InMemorySessionService (默认，开发测试)
            - "sqlite": DatabaseSessionService (SQLite 本地持久化)
            - "database": DatabaseSessionService (自定义数据库 URL)
        db_url: 数据库连接 URL (backend 为 sqlite/database 时使用)
        local_database_path: SQLite 文件路径 (backend 为 sqlite 时使用)

    Examples:
        ```python
        # InMemory (开发)
        stm = ShortTermMemory(backend="local")

        # SQLite 持久化
        stm = ShortTermMemory(backend="sqlite", local_database_path="/tmp/ksadk.db")

        # 从环境变量自动创建
        stm = ShortTermMemory.from_env()
        ```
    """

    backend: Literal["local", "sqlite", "database"] = "local"
    db_url: str = ""
    local_database_path: str = "/tmp/ksadk_local_database.db"

    _session_service: BaseSessionService = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        # 优先使用 db_url
        if self.db_url:
            logger.info(
                f"ShortTermMemory: using db_url (ignoring backend option)"
            )
            self._init_database_service(self.db_url)
            return

        match self.backend:
            case "local":
                self._session_service = InMemorySessionService()
                logger.info("ShortTermMemory: using InMemorySessionService")

            case "sqlite":
                db_url = _normalize_database_url(
                    f"sqlite:///{self.local_database_path}"
                )
                self._init_database_service(db_url)
                logger.info(
                    f"ShortTermMemory: using SQLite at "
                    f"{self.local_database_path}"
                )

            case "database":
                if not self.db_url:
                    raise ValueError(
                        "KSADK_SESSION_DSN is required when ADK session backend resolves to database/postgres"
                    )
                else:
                    self._init_database_service(self.db_url)

            case _:
                logger.warning(
                    f"ShortTermMemory: unknown backend '{self.backend}', "
                    f"falling back to InMemorySessionService"
                )
                self._session_service = InMemorySessionService()

    def _init_database_service(self, db_url: str) -> None:
        """初始化数据库 SessionService"""
        normalized_db_url = _normalize_database_url(db_url)
        try:
            from google.adk.sessions import DatabaseSessionService

            self._session_service = DatabaseSessionService(db_url=normalized_db_url)
            logger.info(
                f"ShortTermMemory: using DatabaseSessionService "
                f"({normalized_db_url[:30]}...)"
            )
        except ImportError:
            logger.warning(
                "DatabaseSessionService not available. "
                "Install google-adk with database support. "
                "Falling back to InMemorySessionService."
            )
            self._session_service = InMemorySessionService()
        except Exception as e:
            logger.error(
                f"Failed to create DatabaseSessionService: {e}. "
                f"Falling back to InMemorySessionService."
            )
            self._session_service = InMemorySessionService()

    @property
    def session_service(self) -> BaseSessionService:
        """获取底层的 SessionService 实例"""
        return self._session_service

    async def create_session(
        self,
        app_name: str,
        user_id: str,
        session_id: str = "",
    ) -> Optional[Session]:
        """创建或获取 session

        如果 session_id 对应的 session 已存在，直接返回。
        否则创建新的 session。

        Args:
            app_name: 应用名称
            user_id: 用户 ID
            session_id: 会话 ID (可选，为空时由 ADK 自动生成)

        Returns:
            Session 对象，创建失败时返回 None
        """
        # 尝试获取已存在的 session
        if session_id:
            try:
                session = await self._session_service.get_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
                if session:
                    logger.debug(
                        f"Session {session_id} already exists "
                        f"(app={app_name}, user={user_id})"
                    )
                    return session
            except Exception as e:
                logger.debug(f"Session {session_id} not found: {e}")

        # 创建新 session
        try:
            if session_id:
                session = await self._session_service.create_session(
                    app_name=app_name,
                    user_id=user_id,
                    session_id=session_id,
                )
            else:
                session = await self._session_service.create_session(
                    app_name=app_name,
                    user_id=user_id,
                )
            logger.info(
                f"Created session: id={session.id}, "
                f"app={app_name}, user={user_id}"
            )
            return session
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            return None

    @classmethod
    def from_env(cls) -> "ShortTermMemory":
        """从环境变量创建 ShortTermMemory

        环境变量:
            KSADK_ADK_SESSION_BACKEND: ADK 专用 backend
            KSADK_ADK_SESSION_URL: ADK 专用数据库 URL
            KSADK_ADK_SESSION_PATH: ADK 专用 SQLite 路径
            KSADK_STM_BACKEND: 平台级 STM backend
            KSADK_STM_URL / KSADK_STM_DB_URL: 平台级数据库 URL
            KSADK_STM_PATH / KSADK_STM_DB_PATH: 平台级 SQLite 路径
            KSADK_SESSION_BACKEND: 统一 session backend fallback
            KSADK_SESSION_DSN: 统一 session DSN fallback
        """
        explicit_backend = _normalize_backend_name(
            _env_first(
                "KSADK_ADK_SESSION_BACKEND",
                "KSADK_STM_BACKEND",
                "KSADK_SESSION_BACKEND",
            )
        )
        db_url = _normalize_database_url(
            _env_first(
                "KSADK_ADK_SESSION_URL",
                "KSADK_STM_URL",
                "KSADK_STM_DB_URL",
                "KSADK_SESSION_DSN",
            )
        )
        configured_db_path = _env_first(
            "KSADK_ADK_SESSION_PATH",
            "KSADK_STM_PATH",
            "KSADK_STM_DB_PATH",
        )
        db_path = configured_db_path or "/tmp/ksadk_local_database.db"

        backend = explicit_backend
        if _session_backend_requires_database_url(backend) and not db_url:
            raise ValueError(
                "KSADK_SESSION_DSN is required when ADK session backend resolves to database/postgres"
            )
        if not backend:
            if db_url:
                backend = "database"
            elif configured_db_path:
                backend = "sqlite"
            else:
                backend = "local"

        return cls(
            backend=backend,
            db_url=db_url,
            local_database_path=db_path,
        )
