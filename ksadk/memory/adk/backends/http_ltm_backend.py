"""HTTP 长期记忆后端 - 远程记忆服务

通过 HTTP API 连接金山云记忆服务。
API 对接细节待提供后实现，当前为框架预留。

环境变量:
    KSADK_LTM_HTTP_URL: 记忆服务 HTTP 地址
    KSADK_LTM_HTTP_TOKEN: 认证 Token
"""

import logging
from typing import List, Optional

import httpx
from pydantic import ConfigDict, Field

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend

logger = logging.getLogger(__name__)


class HttpLTMBackend(BaseLongTermMemoryBackend):
    """HTTP 远程记忆服务后端

    连接金山云记忆服务，通过 HTTP API 进行记忆的存储和检索。

    Attributes:
        base_url: 记忆服务 HTTP 基础地址
        token: 认证 Token
        timeout: HTTP 请求超时时间（秒）

    Examples:
        ```python
        backend = HttpLTMBackend(
            index="my_app",
            base_url="https://memory.ksyun.com/api/v1",
            token="sk-xxx"
        )
        backend.save_memory("user_1", ["用户喜欢Python"])
        results = backend.search_memory("user_1", "编程语言偏好")
        ```
    """

    base_url: str = ""
    token: str = ""
    timeout: int = 30

    _client: Optional[httpx.Client] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_post_init(self, __context) -> None:
        if not self.base_url:
            logger.warning(
                "HttpLTMBackend: base_url is empty. "
                "Set KSADK_LTM_HTTP_URL environment variable."
            )
        logger.info(
            f"HttpLTMBackend initialized: base_url={self.base_url[:50]}... "
            f"index={self.index}"
        )

    @property
    def client(self) -> httpx.Client:
        """懒加载 HTTP 客户端"""
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
            )
        return self._client

    def save_memory(
        self, user_id: str, event_strings: List[str], **kwargs
    ) -> bool:
        """保存记忆到远程服务

        TODO: 对接金山云记忆服务 API
        预期请求格式:
            POST {base_url}/memories
            {
                "index": "...",
                "user_id": "...",
                "events": ["...", "..."]
            }
        """
        if not self.base_url:
            logger.warning("HttpLTMBackend: base_url not configured, skip save.")
            return False

        try:
            payload = {
                "index": self.index,
                "user_id": user_id,
                "events": event_strings,
            }
            payload.update(kwargs)

            response = self.client.post("/memories", json=payload)
            response.raise_for_status()

            logger.info(
                f"Saved {len(event_strings)} events to remote memory service "
                f"for user={user_id}"
            )
            return True

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error saving memory: {e.response.status_code} "
                f"{e.response.text[:200]}"
            )
            return False
        except Exception as e:
            logger.error(f"Error saving memory to remote service: {e}")
            return False

    def search_memory(
        self, user_id: str, query: str, top_k: int = 5, **kwargs
    ) -> List[str]:
        """从远程服务检索记忆

        TODO: 对接金山云记忆服务 API
        预期请求格式:
            POST {base_url}/memories/search
            {
                "index": "...",
                "user_id": "...",
                "query": "...",
                "top_k": 5
            }
        预期响应格式:
            {
                "memories": ["...", "..."]
            }
        """
        if not self.base_url:
            logger.warning(
                "HttpLTMBackend: base_url not configured, return empty results."
            )
            return []

        try:
            payload = {
                "index": self.index,
                "user_id": user_id,
                "query": query,
                "top_k": top_k,
            }
            payload.update(kwargs)

            response = self.client.post("/memories/search", json=payload)
            response.raise_for_status()

            data = response.json()
            memories = data.get("memories", [])

            logger.info(
                f"Retrieved {len(memories)} memories from remote service "
                f"for user={user_id} query='{query[:50]}'"
            )
            return memories

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error searching memory: {e.response.status_code} "
                f"{e.response.text[:200]}"
            )
            return []
        except Exception as e:
            logger.error(f"Error searching memory from remote service: {e}")
            return []

    def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._client:
            self._client.close()
            self._client = None
