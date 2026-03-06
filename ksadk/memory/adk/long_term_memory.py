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
from pydantic import BaseModel, Field
from typing_extensions import Union, override

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend

logger = logging.getLogger(__name__)


def _get_backend_cls(backend: str) -> type:
    """根据名称获取后端类"""
    if backend == "local":
        from ksadk.memory.adk.backends.inmemory_ltm_backend import (
            InMemoryLTMBackend,
        )
        return InMemoryLTMBackend

    if backend == "http":
        from ksadk.memory.adk.backends.http_ltm_backend import (
            HttpLTMBackend,
        )
        return HttpLTMBackend

    if backend == "sdk":
        from ksadk.memory.adk.backends.sdk_ltm_backend import (
            SdkLTMBackend,
        )
        return SdkLTMBackend

    raise ValueError(
        f"Unsupported long term memory backend: {backend}. "
        f"Available: local, http, sdk"
    )


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

    class Config:
        arbitrary_types_allowed = True

    def model_post_init(self, __context: Any) -> None:
        # 情况 1: 用户直接传入了 backend 实例
        if isinstance(self.backend, BaseLongTermMemoryBackend):
            self._backend = self.backend
            self.index = self._backend.index
            logger.info(
                f"LongTermMemory initialized with provided backend: "
                f"{self._backend.__class__.__name__}, index={self.index}"
            )
            return

        # 确定 index
        self.index = self.index or self.app_name
        if not self.index:
            self.index = "default_app"
            logger.warning(
                "LongTermMemory: index and app_name both empty, "
                "using 'default_app'"
            )

        # 情况 2: 用户传入了 backend_config
        if self.backend_config:
            if "index" not in self.backend_config:
                self.backend_config["index"] = self.index

            backend_cls = _get_backend_cls(self.backend)
            self._backend = backend_cls(**self.backend_config)
            logger.info(
                f"LongTermMemory initialized: backend={self.backend}, "
                f"index={self.index}"
            )
            return

        # 情况 3: 仅指定 backend 名称，使用默认配置
        backend_cls = _get_backend_cls(self.backend)
        self._backend = backend_cls(index=self.index)
        logger.info(
            f"LongTermMemory initialized: backend={self.backend}, "
            f"index={self.index}"
        )

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

        self._backend.save_memory(
            user_id=user_id,
            event_strings=event_strings,
            **kwargs,
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
            memory_chunks = self._backend.search_memory(
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
    def from_env(cls) -> "LongTermMemory":
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
        backend = os.environ.get("KSADK_LTM_BACKEND", "local")
        top_k = int(os.environ.get("KSADK_LTM_TOP_K", "5"))
        index = os.environ.get("KSADK_LTM_INDEX", "")
        app_name = os.environ.get("KSADK_LTM_APP_NAME", "")

        backend_config = {}
        if backend == "http":
            backend_config = {
                "base_url": os.environ.get("KSADK_LTM_HTTP_URL", ""),
                "token": os.environ.get("KSADK_LTM_HTTP_TOKEN", ""),
            }
        elif backend == "sdk":
            backend_config = {
                "access_key": (
                    os.environ.get("KSADK_LTM_ACCESS_KEY")
                    or os.environ.get("KSYUN_ACCESS_KEY", "")
                ),
                "secret_key": (
                    os.environ.get("KSADK_LTM_SECRET_KEY")
                    or os.environ.get("KSYUN_SECRET_KEY", "")
                ),
                "region": os.environ.get("KSADK_LTM_REGION", "cn-north-vip1"),
                "endpoint": os.environ.get("KSADK_LTM_ENDPOINT", "aicp.api.ksyun.com"),
                "scheme": os.environ.get("KSADK_LTM_SCHEME", "https"),
                "namespace": os.environ.get("KSADK_LTM_NAMESPACE", ""),
                "agent_id": os.environ.get("KSADK_LTM_AGENT_ID", ""),
                "scene_id": os.environ.get("KSADK_LTM_SCENE_ID", ""),
            }

        return cls(
            backend=backend,
            backend_config=backend_config,
            top_k=top_k,
            index=index,
            app_name=app_name,
        )
