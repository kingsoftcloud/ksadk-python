"""LongTermMemory - 长期记忆服务

实现 Google ADK 的 BaseMemoryService 接口，提供跨 session 的记忆存储和检索。
通过环境变量配置后端类型和参数，由 ADKRunner 自动注入。

参考 VeADK: veadk/memory/long_term_memory.py

环境变量:
    KSADK_LTM_BACKEND: 后端类型 (local / http)
    KSADK_LTM_HTTP_URL: HTTP 记忆服务地址
    KSADK_LTM_HTTP_TOKEN: HTTP 认证 Token
    KSADK_LTM_TOP_K: 检索数量 (默认 5)

使用示例:
    # 通过环境变量自动配置 (推荐)
    # 设置 KSADK_LTM_BACKEND=local 即可

    # 手动创建
    from ksadk.memory.adk import LongTermMemory
    ltm = LongTermMemory(backend="local", app_name="my_app")
"""

import json
import logging
import os
from typing import Any, List, Literal

from google.adk.events.event import Event
from google.adk.memory.base_memory_service import (
    BaseMemoryService,
    SearchMemoryResponse,
)
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions import Session
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Union, override

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend
from ksadk.memory.service import LongTermMemoryService

logger = logging.getLogger(__name__)


class LongTermMemory(BaseMemoryService, BaseModel):
    """长期记忆服务 - 跨 session 的记忆管理

    实现 Google ADK 的 BaseMemoryService 接口，支持:
    - 将 session 中的用户消息持久化到记忆后端
    - 通过语义/关键词检索历史记忆

    Attributes:
        backend: 后端类型或实例 ("local" / "http" / BaseLongTermMemoryBackend)
        backend_config: 后端配置参数
        top_k: 检索返回的最大记忆条数
        index: 索引名称 (用于隔离不同应用的数据)
        app_name: 应用名称 (备选 index)

    Examples:
        ```python
        # 本地开发
        ltm = LongTermMemory(backend="local", app_name="my_app")

        # 远程服务
        ltm = LongTermMemory(
            backend="http",
            backend_config={
                "base_url": "https://memory.ksyun.com/api/v1",
                "token": "sk-xxx",
            },
            app_name="my_app"
        )
        ```
    """

    backend: Union[
        Literal["local", "http", "sdk"],
        BaseLongTermMemoryBackend,
    ] = "local"

    backend_config: dict = Field(default_factory=dict)
    top_k: int = 5
    index: str = ""
    app_name: str = ""

    _backend: BaseLongTermMemoryBackend = None
    _service: LongTermMemoryService = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_post_init(self, __context: Any) -> None:
        self._service = LongTermMemoryService(
            backend=self.backend,
            backend_config=self.backend_config,
            top_k=self.top_k,
            index=self.index,
            app_name=self.app_name,
        )
        self._backend = self._service._backend
        self.index = self._service.index
        logger.info(
            "LongTermMemory initialized: backend=%s, index=%s",
            self.backend,
            self.index,
        )

    def _get_service(self) -> LongTermMemoryService:
        if self._service is None:
            self.model_post_init(None)
        if self._backend is not None and self._service._backend is not self._backend:
            self._service.backend = self._backend
            self._service._backend = self._backend
            self._service.index = getattr(self._backend, "index", self._service.index)
            self.index = self._service.index
        return self._service

    def _filter_and_convert_events(self, events: List[Event]) -> List[str]:
        """过滤并序列化 session 事件

        参考 VeADK 的逻辑:
        - 只保留用户消息 (author == "user")
        - 过滤掉空内容、function call、function response
        - 序列化为 JSON 字符串
        """
        final_events = []
        for event in events:
            # 过滤: 无内容的事件
            if not event.content or not event.content.parts:
                continue

            # 过滤: 只保留用户事件
            if event.author != "user":
                continue

            # 过滤: 去除 function call / function response (无 text)
            if not event.content.parts[0].text:
                continue

            # 转换: 序列化为 JSON
            message = event.content.model_dump(exclude_none=True, mode="json")
            final_events.append(json.dumps(message, ensure_ascii=False))

        return final_events

    @override
    async def add_session_to_memory(
        self,
        session: Session,
        **kwargs,
    ):
        """将 session 中的用户消息保存到长期记忆

        Args:
            session: Google ADK Session 对象
        """
        user_id = session.user_id
        event_strings = self._filter_and_convert_events(session.events)

        if not event_strings:
            logger.info(
                f"No user events to save for session={session.id}"
            )
            return

        logger.info(
            f"Saving {len(event_strings)} events to long term memory: "
            f"index={self.index}, user_id={user_id}"
        )

        self._get_service().save_event_strings(
            user_id=user_id,
            event_strings=event_strings,
            metadata=kwargs.get("metadata"),
        )

        logger.info(
            f"Saved {len(event_strings)} events to long term memory: "
            f"index={self.index}, user_id={user_id}"
        )

    @override
    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        """检索长期记忆

        Args:
            app_name: 应用名称
            user_id: 用户 ID
            query: 查询文本

        Returns:
            SearchMemoryResponse: 包含匹配记忆条目的响应
        """
        logger.info(f"Searching memory: query='{query[:50]}' user_id={user_id}")

        memory_chunks = []
        try:
            memory_chunks = self._get_service().search_entries(
                query=query, top_k=self.top_k, user_id=user_id
            )
        except Exception as e:
            logger.error(
                f"Error during memory search: {e}. Returning empty results."
            )

        # 转换为 MemoryEntry 格式
        memory_events = []
        for memory in memory_chunks:
            try:
                memory_dict = json.loads(memory)
                try:
                    text = memory_dict["parts"][0]["text"]
                    role = memory_dict.get("role", "user")
                except (KeyError, IndexError):
                    logger.warning(
                        f"Non-standard memory format: {memory[:100]}. Skipping."
                    )
                    continue
            except json.JSONDecodeError:
                # 非 JSON 格式的记忆字符串，直接作为 text
                text = memory
                role = "user"

            memory_events.append(
                MemoryEntry(
                    author="user",
                    content=types.Content(
                        parts=[types.Part(text=text)], role=role
                    ),
                )
            )

        logger.info(
            f"Found {len(memory_events)} memory entries for "
            f"query='{query[:50]}' index={self.index} user_id={user_id}"
        )
        return SearchMemoryResponse(memories=memory_events)

    @classmethod
    def from_env(
        cls,
        *,
        app_name: str = "",
        backend: str | None = None,
    ) -> "LongTermMemory":
        """从环境变量创建 LongTermMemory

        环境变量:
            KSADK_LTM_BACKEND: local / http / sdk
            KSADK_LTM_HTTP_URL: HTTP 记忆服务地址
            KSADK_LTM_HTTP_TOKEN: HTTP 认证 Token
            KSADK_LTM_ACCESS_KEY: SDK AK (fallback to KSYUN_ACCESS_KEY)
            KSADK_LTM_SECRET_KEY: SDK SK (fallback to KSYUN_SECRET_KEY)
            KSADK_LTM_TOP_K: 检索数量 (默认 5)
            KSADK_LTM_INDEX: 索引名称
        """
        service = LongTermMemoryService.from_env(app_name=app_name, backend=backend)
        backend_name = (
            backend
            or os.environ.get("KSADK_LTM_BACKEND", "local")
        )
        backend_config = dict(service.backend_config)
        if "index" in backend_config and backend_name != "local":
            backend_config.pop("index", None)

        return cls(
            backend=backend_name,
            backend_config=backend_config,
            top_k=service.top_k,
            index=service.index,
            app_name=service.app_name,
        )
