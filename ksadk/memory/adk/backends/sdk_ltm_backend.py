"""SDK 长期记忆后端 - 金山云 AICP 记忆库

通过 kingsoftcloud-sdk-python 调用 AICP 记忆库 API：
  - CreateMemorySdk: 向指定记忆库写入记忆
  - QueryMemorySdk: 从记忆库检索记忆

参照 KnowledgeBaseClient (ksadk/knowledge_base/client.py) 的模式实现。

环境变量:
    KSADK_LTM_BACKEND: sdk
    KSADK_LTM_ACCESS_KEY: AK (可选，默认取 KSYUN_ACCESS_KEY)
    KSADK_LTM_SECRET_KEY: SK (可选，默认取 KSYUN_SECRET_KEY)
    KSADK_LTM_REGION: 区域 (默认 cn-beijing-6)
    KSADK_LTM_ENDPOINT: API 端点 (默认 aicp.api.ksyun.com)
    KSADK_LTM_SCHEME: http/https (默认 https)
    KSADK_LTM_NAMESPACE: 记忆库数据面 Namespace
    KSADK_LTM_AGENT_ID: Agent ID
    KSADK_LTM_SCENE_ID: 场景 ID (默认 _sys_general)
"""

import json
import logging
import time
import uuid
from typing import Any

from pydantic import ConfigDict, Field

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend

logger = logging.getLogger(__name__)

DEFAULT_SCENE_ID = "_sys_general"


class SdkLTMBackend(BaseLongTermMemoryBackend):
    """金山云 AICP 记忆库 SDK 后端

    通过 kingsoftcloud SDK 调用 CreateMemorySdk / QueryMemorySdk API，
    实现记忆的云端持久化和语义检索。

    Attributes:
        access_key: 访问密钥 ID (AK)
        secret_key: 访问密钥 (SK)
        region: API 区域
        endpoint: API 端点
        scheme: http 或 https
        namespace: 记忆库数据面 Namespace
        agent_id: Agent ID
        scene_id: 场景 ID

    Examples:
        ```python
        backend = SdkLTMBackend(
            index="my_app",
            access_key="ak",
            secret_key="sk",
            namespace="my_namespace",
        )
        backend.save_memory("user_1", ["用户喜欢Python"])
        results = backend.search_memory("user_1", "编程语言偏好")
        ```
    """

    access_key: str = ""
    secret_key: str = ""
    region: str = "cn-beijing-6"
    endpoint: str = "aicp.api.ksyun.com"
    scheme: str = "https"
    namespace: str = ""
    memory_collection_id: str = ""
    agent_id: str = ""
    scene_id: str = DEFAULT_SCENE_ID
    last_error: str = ""
    last_create_response: dict[str, Any] = Field(default_factory=dict)
    last_session_status: dict[str, Any] = Field(default_factory=dict)

    _aicp_client: Any = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def model_post_init(self, __context) -> None:
        if not self.access_key or not self.secret_key:
            logger.warning(
                "SdkLTMBackend: AK/SK not provided. "
                "Set KSADK_LTM_ACCESS_KEY/KSADK_LTM_SECRET_KEY "
                "or KSYUN_ACCESS_KEY/KSYUN_SECRET_KEY."
            )
        logger.info(
            f"SdkLTMBackend initialized: "
            f"endpoint={self.endpoint}, region={self.region}, "
            f"memory_collection_id={self._effective_memory_collection_id()}, "
            f"agent_id={self.agent_id}"
        )

    def _get_client(self):
        """懒加载 AICP 客户端

        参照 KnowledgeBaseClient._get_client() 的模式:
        - 导入 credential / HttpProfile / ClientProfile
        - 多版本 fallback: v20251114 → v20251212 → v20240612
        - 设置 _apiVersion = "2025-11-14"
        """
        if self._aicp_client is not None:
            return self._aicp_client

        try:
            from ksyun.common import credential
            from ksyun.common.profile.client_profile import ClientProfile
            from ksyun.common.profile.http_profile import HttpProfile
        except ImportError:
            raise ImportError(
                "kingsoftcloud-sdk-python is required for SDK memory backend. "
                "Install it with: pip install 'kingsoftcloud-sdk-python>=1.5.8.94'"
            )

        # 多版本 fallback
        aicp_module = None
        for version in ["v20251114", "v20251212", "v20240612"]:
            try:
                aicp_module = __import__(
                    f"ksyun.client.aicp.{version}.client",
                    fromlist=["AicpClient"],
                )
                logger.debug(f"Using aicp client version: {version}")
                break
            except ImportError:
                continue

        if aicp_module is None:
            raise ImportError(
                "Cannot import ksyun.client.aicp client. "
                "Ensure kingsoftcloud-sdk-python>=1.5.8.94 is installed."
            )

        cred = credential.Credential(self.access_key, self.secret_key)

        http_profile = HttpProfile()
        http_profile.endpoint = self.endpoint
        http_profile.reqMethod = "POST"
        http_profile.reqTimeout = 60
        http_profile.scheme = self.scheme

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        self._aicp_client = aicp_module.AicpClient(
            cred, self.region, profile=client_profile
        )

        # 强制覆写 API 版本为记忆库 API 所需的 2025-11-14
        self._aicp_client._apiVersion = "2025-11-14"

        logger.info(
            f"SdkLTMBackend AICP client initialized: "
            f"endpoint={self.endpoint}, region={self.region}"
        )
        return self._aicp_client

    def _build_conversation(self, event_strings: list[str]) -> list:
        """将事件字符串列表转换为 Conversation 格式

        每个 event_string 是 JSON: {"role":"user","parts":[{"text":"..."}]}
        转换为 API 要求的:
            {"Role":"user","CreatedAt":ms,"MessageId":"uuid","Content":[{"Type":"input_text","Text":"..."}]}
        """
        conversation = []
        for event_str in event_strings:
            try:
                event = json.loads(event_str)
                role = event.get("role", "user")
                text = ""
                parts = event.get("parts", [])
                if parts and isinstance(parts[0], dict):
                    text = parts[0].get("text", "")
            except (json.JSONDecodeError, TypeError):
                # 非 JSON 格式，直接当作纯文本
                role = "user"
                text = str(event_str)

            if not text:
                continue

            conversation.append({
                "Role": role,
                "CreatedAt": int(time.time() * 1000),
                "MessageId": str(uuid.uuid4()),
                "Content": [{"Type": "input_text", "Text": text}],
            })
        return conversation

    def _effective_memory_collection_id(self) -> str:
        return self.memory_collection_id or self.namespace or self.index

    def _effective_scene_id(self) -> str:
        return self.scene_id or DEFAULT_SCENE_ID

    def save_memory(
        self, user_id: str, event_strings: list[str], **kwargs
    ) -> bool:
        """调用 CreateMemorySdk 写入记忆

        Args:
            user_id: 用户 ID
            event_strings: 序列化的事件字符串列表
            **kwargs: 可选参数

        Returns:
            是否保存成功
        """
        if not event_strings:
            return True

        client = self._get_client()
        memory_collection_id = self._effective_memory_collection_id()

        try:
            self.last_error = ""
            conversation = self._build_conversation(event_strings)
            if not conversation:
                logger.info("No valid conversation items to save")
                return True

            metadata = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
            agent_id = metadata.get("agent_id") or self.agent_id
            session_id = metadata.get("session_id") or kwargs.get("session_id")
            params = {
                "MemoryCollectionId": memory_collection_id,
                "AgentUserId": user_id,
                "SceneId": self._effective_scene_id(),
                "DataType": "conversation",
                "Data": {"Conversation": conversation},
            }
            if agent_id:
                params["AgentId"] = agent_id
            if session_id:
                params["SessionId"] = session_id

            logger.info(
                f"CreateMemorySdk: memory_collection_id={memory_collection_id}, "
                f"user_id={user_id}, messages={len(conversation)}"
            )

            response = client.call(
                "CreateMemorySdk", params, options={"IsPostJson": True}
            )
            self.last_create_response = self._parse_json_response(response) or {}
            self.last_session_status = {
                "SessionId": session_id,
                "AgentUserId": user_id,
                "MemoryCollectionId": memory_collection_id,
            }

            logger.info(
                f"Saved {len(conversation)} messages to AICP memory service "
                f"for user={user_id}"
            )
            return True

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"CreateMemorySdk failed: {e}")
            return False

    def search_memory(
        self, user_id: str, query: str, top_k: int = 5, **kwargs
    ) -> list[str]:
        """调用 QueryMemorySdk 检索记忆

        Args:
            user_id: 用户 ID
            query: 查询文本 (语义检索)
            top_k: 返回最相关的 N 条记忆
            **kwargs: 可选参数

        Returns:
            匹配的记忆字符串列表
        """
        client = self._get_client()
        memory_collection_id = self._effective_memory_collection_id()

        try:
            self.last_error = ""
            params = {
                "MemoryCollectionId": memory_collection_id,
                "AgentUserId": user_id,
                "Query": query,
                "Limit": top_k,
                "SceneId": self._effective_scene_id(),
            }

            # 可选参数
            if kwargs.get("occurred_after"):
                params["OccurredAfter"] = kwargs["occurred_after"]
            if kwargs.get("occurred_before"):
                params["OccurredBefore"] = kwargs["occurred_before"]
            if kwargs.get("mode"):
                params["Mode"] = kwargs["mode"]
            if kwargs.get("return_citations") is not None:
                params["ReturnCitations"] = kwargs["return_citations"]
            if kwargs.get("scene_ids"):
                params["SceneIds"] = kwargs["scene_ids"]

            logger.info(
                f"QueryMemorySdk: memory_collection_id={memory_collection_id}, "
                f"user_id={user_id}, query='{query[:50]}'"
            )

            response = client.call(
                "QueryMemorySdk", params, options={"IsPostJson": True}
            )

            # 解析响应
            memories = self._parse_query_response(response)

            logger.info(
                f"Retrieved {len(memories)} memories from AICP memory service "
                f"for user={user_id} query='{query[:50]}'"
            )
            return memories

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"QueryMemorySdk failed: {e}")
            return []

    def get_session_status(
        self,
        *,
        user_id: str,
        session_id: str,
        page_size: int = 20,
    ) -> dict[str, Any] | None:
        """Return raw AICP session status for a recently submitted memory session."""
        if not session_id:
            return None

        client = self._get_client()
        memory_collection_id = self._effective_memory_collection_id()
        params = {
            "MemoryCollectionId": memory_collection_id,
            "AgentUserId": user_id,
            "Page": 1,
            "PageSize": page_size,
        }

        try:
            response = client.call("ListSessions", params, options={"IsPostJson": True})
            data = self._parse_json_response(response)
        except Exception as e:
            self.last_error = str(e)
            logger.warning(f"ListSessions failed while checking memory status: {e}")
            return None

        payload = data.get("Data") if isinstance(data, dict) else None
        items = payload.get("Items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return None

        for item in items:
            if isinstance(item, dict) and item.get("SessionId") == session_id:
                self.last_session_status = item
                return item
        return None

    def _parse_query_response(self, response: str) -> list[str]:
        """解析 QueryMemorySdk 响应

        响应格式待 API 文档确认后完善。
        当前按通用格式解析，兼容多种可能的返回结构。
        """
        try:
            data = self._parse_json_response(response)
        except (json.JSONDecodeError, TypeError):
            logger.error(f"Failed to parse QueryMemorySdk response: {str(response)[:200]}")
            return []
        if not isinstance(data, dict):
            logger.error(f"Failed to parse QueryMemorySdk response: {str(response)[:200]}")
            return []

        # 尝试多种可能的响应字段名
        memories = []

        # 格式 1: {"Memories": [...]}
        if "Memories" in data:
            raw_memories = data["Memories"]
            for item in raw_memories:
                if isinstance(item, str):
                    memories.append(item)
                elif isinstance(item, dict):
                    # 优先取 Content 字段，备选 Text / Data
                    text = (
                        item.get("Content")
                        or item.get("Text")
                        or item.get("Memory")
                        or item.get("Data")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    memories.append(text)

        # 格式 2: {"Data": [...]}
        elif "Data" in data and isinstance(data["Data"], list):
            for item in data["Data"]:
                memories.extend(self._parse_memory_item(item))

        # 格式 3: {"Results": [...]}
        elif "Results" in data:
            for item in data["Results"]:
                if isinstance(item, str):
                    memories.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("Content")
                        or item.get("Text")
                        or item.get("Memory")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    memories.append(text)

        else:
            # 无法识别的响应格式，记录日志
            logger.warning(
                f"Unknown QueryMemorySdk response format. "
                f"Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

        return memories

    def _parse_json_response(self, response: Any) -> Any:
        if isinstance(response, str):
            return json.loads(response)
        return response

    def _parse_memory_item(self, item: Any) -> list[str]:
        if isinstance(item, str):
            return [item]
        if not isinstance(item, dict):
            return []

        if isinstance(item.get("Memories"), list):
            parsed: list[str] = []
            for memory in item["Memories"]:
                parsed.extend(self._parse_memory_item(memory))
            return parsed

        text = (
            item.get("Content")
            or item.get("Text")
            or item.get("Memory")
            or item.get("Data")
        )
        if text is None:
            return []
        if isinstance(text, (dict, list)):
            return [json.dumps(text, ensure_ascii=False)]
        return [str(text)]
