"""SDK 长期记忆后端 - 金山云 AICP 记忆库

通过 kingsoftcloud-sdk-python 调用 AICP 记忆库 API：
  - CreateMemorySdk: 向指定记忆库写入记忆
  - QueryMemorySdk: 从记忆库检索记忆

参照 KnowledgeBaseClient (ksadk/knowledge_base/client.py) 的模式实现。

环境变量:
    KSADK_LTM_BACKEND: sdk
    KSADK_LTM_ACCESS_KEY: AK (可选，默认取 KSYUN_ACCESS_KEY / KSYUN_ACCESS_KEY_ID)
    KSADK_LTM_SECRET_KEY: SK (可选，默认取 KSYUN_SECRET_KEY / KSYUN_SECRET_ACCESS_KEY)
    KSADK_LTM_SESSION_TOKEN: STS 临时会话 token (可选，默认取 KSYUN_SESSION_TOKEN)
    KSADK_LTM_REGION: 区域 (默认 cn-beijing-6)
    KSADK_LTM_ENDPOINT: API 端点 (默认 aicp.api.ksyun.com)
    KSADK_LTM_SCHEME: http/https (默认 https)
    KSADK_LTM_NAMESPACE: 记忆库数据面 Namespace
    KSADK_LTM_AGENT_ID: Agent ID
    KSADK_LTM_SCENE_ID: 场景 ID (默认 _sys_general)
"""

import json
import logging
import re
import time
import uuid
from typing import Any, List, Set

from pydantic import ConfigDict, Field

from ksadk.memory.adk.backends.base_ltm_backend import (
    CAP_ADD,
    CAP_DELETE,
    CAP_FLUSH,
    CAP_SEARCH,
    CAP_SESSION_STATUS,
    CAP_STRUCTURED_SEARCH,
    CAP_UPDATE,
    BaseLongTermMemoryBackend,
)
from ksadk.memory.models import (
    LongTermMemoryRecord,
    MemoryExtractionStatus,
    MemoryMutationResult,
    map_session_state,
)

logger = logging.getLogger(__name__)

DEFAULT_SCENE_ID = "_sys_general"

# "记忆不存在"识别模式（方案 §17.4：准确错误码待真实 fixture 固化，
# 首版按保守中英文模式匹配，fixture 到位后收敛为精确匹配）。
_NOT_EXIST_RE = re.compile(
    r"not[ _]?exist|does not exist|memory.*不存在|记忆不存在|记忆已被删除|resourcenotfound|notfound",
    re.IGNORECASE,
)


class SdkLTMBackend(BaseLongTermMemoryBackend):
    """金山云 AICP 记忆库 SDK 后端

    通过 kingsoftcloud SDK 调用 CreateMemorySdk / QueryMemorySdk API，
    实现记忆的云端持久化和语义检索。

    Attributes:
        access_key: 访问密钥 ID (AK)
        secret_key: 访问密钥 (SK)
        session_token: STS 临时会话 token
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
    session_token: str = ""
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
            from ksyun.common import credential  # type: ignore[import-untyped]
            from ksyun.common.profile.client_profile import (  # type: ignore[import-untyped]
                ClientProfile,
            )
            from ksyun.common.profile.http_profile import (  # type: ignore[import-untyped]
                HttpProfile,
            )
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

        cred = credential.Credential(
            self.access_key, self.secret_key, self.session_token or None
        )

        http_profile = HttpProfile()
        http_profile.endpoint = self.endpoint
        http_profile.reqMethod = "POST"
        http_profile.reqTimeout = 60
        http_profile.scheme = self.scheme

        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile

        self._aicp_client = aicp_module.AicpClient(cred, self.region, profile=client_profile)

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

            conversation.append(
                {
                    "Role": role,
                    "CreatedAt": int(time.time() * 1000),
                    "MessageId": str(uuid.uuid4()),
                    "Content": [{"Type": "input_text", "Text": text}],
                }
            )
        return conversation

    def _effective_memory_collection_id(self) -> str:
        return self.memory_collection_id or self.namespace or self.index

    def _effective_scene_id(self) -> str:
        return self.scene_id or DEFAULT_SCENE_ID

    def save_memory(self, user_id: str, event_strings: list[str], **kwargs) -> bool:
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

            metadata_value = kwargs.get("metadata")
            metadata: dict[str, Any] = (
                dict(metadata_value) if isinstance(metadata_value, dict) else {}
            )
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
            if metadata.get("flush") is True or kwargs.get("flush") is True:
                params["Flush"] = True

            logger.info(
                f"CreateMemorySdk: memory_collection_id={memory_collection_id}, "
                f"user_id={user_id}, messages={len(conversation)}"
            )

            response = client.call("CreateMemorySdk", params, options={"IsPostJson": True})
            self.last_create_response = self._parse_json_response(response) or {}
            self.last_session_status = {
                "SessionId": session_id,
                "AgentUserId": user_id,
                "MemoryCollectionId": memory_collection_id,
            }

            logger.info(
                f"Saved {len(conversation)} messages to AICP memory service " f"for user={user_id}"
            )
            return True

        except Exception as e:
            self.last_error = str(e)
            logger.error(f"CreateMemorySdk failed: {e}")
            return False

    def search_memory(self, user_id: str, query: str, top_k: int = 5, **kwargs) -> list[str]:
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

            response = client.call("QueryMemorySdk", params, options={"IsPostJson": True})

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

    # ------------------------------------------------------------------
    # 结构化扩展协议（方案 §7.3/§7.4）：走通用 client.call 通道。
    # kingsoftcloud-sdk-python 1.5.8.101 的 AICP client 未提供
    # ListMemories/UpdateMemory/DeleteMemory 类型化方法（§7.4.2）。
    # 所有解析对已确认 schema 严格校验；未知结构 fail closed，不伪造结果。
    # ------------------------------------------------------------------

    def search_records(
        self, user_id: str, query: str, top_k: int = 5, **kwargs
    ) -> List[LongTermMemoryRecord]:
        """QueryMemorySdk 结构化检索：返回带服务端 MemoryId 的记录。

        按已确认 schema（§7.4.1）从 ``Data[].Memories[]`` 解析
        MemoryId / Memory / Score / OccurredStart / OccurredEnd。
        缺少 MemoryId 的条目跳过（无法支撑后续 mutation）。
        未知响应结构返回空列表（fail closed）。
        """
        client = self._get_client()
        memory_collection_id = self._effective_memory_collection_id()
        params = {
            "MemoryCollectionId": memory_collection_id,
            "AgentUserId": user_id,
            "Query": query,
            "Limit": top_k,
            "SceneId": self._effective_scene_id(),
        }
        response = client.call("QueryMemorySdk", params, options={"IsPostJson": True})
        records = self._parse_query_records_response(response, user_id=user_id)
        logger.info(
            f"QueryMemorySdk structured: user={user_id}, records={len(records)}"
        )
        return records

    def list_memory_records(
        self,
        *,
        user_id: str,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> List[LongTermMemoryRecord]:
        """ListMemories：精确确认提取完成后的可见性 / 取得当前 MemoryId。

        按已确认 schema（§7.4.1）从顶层 ``MemoryList[]`` 解析。
        未知响应结构返回空列表（fail closed，不宣称可见）。
        """
        client = self._get_client()
        params: dict[str, Any] = {
            "MemoryCollectionId": self._effective_memory_collection_id(),
            "AgentUserId": user_id,
            "Page": page,
            "PageSize": page_size,
        }
        if query:
            params["Query"] = query
        response = client.call("ListMemories", params, options={"IsPostJson": True})
        records = self._parse_list_memories_response(response, user_id=user_id)
        logger.info(f"ListMemories: user={user_id}, records={len(records)}")
        return records

    def update_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        **kwargs,
    ) -> MemoryMutationResult:
        """UpdateMemory：按 ID 原地更新正文，解析 new_memory_id。

        Update 不幂等：重复调用会重复触发（§17.7），由上层控制重试。
        "记忆不存在"按 not_found 归一（§7.4.1，错误码待 fixture 收敛）。
        """
        client = self._get_client()
        params = {
            "MemoryCollectionId": self._effective_memory_collection_id(),
            "MemoryId": memory_id,
            "Content": content,
            "AgentUserId": user_id,
        }
        try:
            response = client.call("UpdateMemory", params, options={"IsPostJson": True})
        except Exception as exc:
            if self._is_not_exist_error(exc):
                return MemoryMutationResult(
                    ok=False,
                    memory_id=memory_id,
                    status="not_found",
                    message="目标记忆不存在",
                )
            self.last_error = str(exc)
            logger.error("UpdateMemory failed: %s", type(exc).__name__)
            return MemoryMutationResult(
                ok=False,
                memory_id=memory_id,
                status="failed",
                message="记忆更新失败",
            )

        data = self._parse_json_response(response)
        response_memory_id = self._parse_new_memory_id(data)
        new_memory_id = response_memory_id
        if not response_memory_id or response_memory_id == memory_id:
            # The service can merge an edited memory into another record while
            # returning no new ID (or echoing the old one).  Do not expose that
            # stale handle to callers: confirm the current handle by listing
            # records whose final content exactly matches the update.
            try:
                matches = [
                    record
                    for record in self.list_memory_records(
                        user_id=user_id,
                        query=content,
                        page=1,
                        page_size=100,
                    )
                    if record.content.strip() == content.strip()
                ]
            except Exception as exc:
                logger.warning(
                    "ListMemories failed while reconciling updated memory ID: %s",
                    type(exc).__name__,
                )
                matches = []
            unique_ids = {record.memory_id for record in matches}
            new_memory_id = unique_ids.pop() if len(unique_ids) == 1 else ""

        if new_memory_id and new_memory_id != memory_id:
            message = f"更新成功，新记忆 ID: {new_memory_id}"
        elif new_memory_id == memory_id:
            message = "更新成功，记忆 ID 未变化"
        else:
            message = "更新成功，但未能唯一确认更新后的记忆 ID，请重新搜索后再操作"
        return MemoryMutationResult(
            ok=True,
            memory_id=memory_id,
            new_memory_id=new_memory_id,
            status="updated",
            message=message,
        )

    def delete_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        **kwargs,
    ) -> MemoryMutationResult:
        """DeleteMemory：按 ID 软删除。

        重复删除返回"记忆不存在"，归一为 already_absent，
        与首次成功 deleted 分开审计（§7.4）。
        """
        client = self._get_client()
        params = {
            "MemoryCollectionId": self._effective_memory_collection_id(),
            "MemoryId": memory_id,
            "AgentUserId": user_id,
        }
        try:
            client.call("DeleteMemory", params, options={"IsPostJson": True})
        except Exception as exc:
            if self._is_not_exist_error(exc):
                return MemoryMutationResult(
                    ok=True,
                    memory_id=memory_id,
                    status="already_absent",
                    message="目标记忆已不存在（可能已删除）",
                )
            self.last_error = str(exc)
            logger.error("DeleteMemory failed: %s", type(exc).__name__)
            return MemoryMutationResult(
                ok=False,
                memory_id=memory_id,
                status="failed",
                message="记忆删除失败",
            )
        return MemoryMutationResult(
            ok=True,
            memory_id=memory_id,
            status="deleted",
            message="已删除",
        )

    def get_extraction_status(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> MemoryExtractionStatus:
        """ListSessions 查询 Session 后台提取状态（§7.7）。

        State 映射 0/50/100/-50/-100；未找到 Session 返回 unknown。
        searchable 需 Service 层结合 ListMemories 确认后置位。
        """
        item = self.get_session_status(user_id=user_id, session_id=session_id)
        if not isinstance(item, dict):
            return MemoryExtractionStatus(
                session_id=session_id,
                state=None,
                status="unknown",
                message="Session 状态未知",
            )
        state = item.get("State")
        state_int = int(state) if isinstance(state, (int, float, str)) and str(state).lstrip("-").isdigit() else None
        status = map_session_state(state_int)
        message = {
            "queued": "排队中",
            "extracting": "提取中",
            "extracted": "提取完成",
            "duplicate_skipped": "内容重复，已跳过提取",
            "failed": "提取失败，可稍后重新明确保存",
        }.get(status, "状态未知")
        return MemoryExtractionStatus(
            session_id=session_id,
            state=state_int,
            status=status,
            message=message,
        )

    def capabilities(self) -> Set[str]:
        return {
            CAP_SEARCH,
            CAP_ADD,
            CAP_FLUSH,
            CAP_STRUCTURED_SEARCH,
            CAP_UPDATE,
            CAP_DELETE,
            CAP_SESSION_STATUS,
        }

    @staticmethod
    def _is_not_exist_error(exc: Exception) -> bool:
        """识别"记忆不存在"类错误。

        §17.4：准确错误码/结构待真实 fixture 固化；首版按保守模式匹配
        code/message，fixture 到位后收敛为精确匹配。
        """
        text = " ".join(
            str(part) for part in (getattr(exc, "code", ""), str(exc)) if part
        )
        return bool(_NOT_EXIST_RE.search(text))

    @staticmethod
    def _parse_new_memory_id(data: Any) -> str:
        """从 UpdateMemory 响应解析 new_memory_id（§7.4.1）。

        兼容 snake/camel 两种命名；都不存在时返回空串，由调用方通过
        ListMemories 核验当前句柄，不能据此推断 ID 未变化。
        """
        if not isinstance(data, dict):
            return ""
        for key in ("new_memory_id", "NewMemoryId", "NewMemoryID"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = data.get("Data")
        if isinstance(nested, dict):
            for key in ("new_memory_id", "NewMemoryId", "NewMemoryID"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _parse_query_records_response(
        self, response: Any, *, user_id: str
    ) -> List[LongTermMemoryRecord]:
        """严格解析 QueryMemorySdk 结构化响应：Data[].Memories[]。"""
        try:
            data = self._parse_json_response(response)
        except (json.JSONDecodeError, TypeError):
            logger.error("QueryMemorySdk records: invalid JSON response")
            return []
        if not isinstance(data, dict):
            logger.error("QueryMemorySdk records: unexpected payload type")
            return []
        items = data.get("Data")
        if not isinstance(items, list):
            logger.warning(
                "QueryMemorySdk records: unknown schema, keys=%s; fail closed",
                list(data.keys()),
            )
            return []
        records: List[LongTermMemoryRecord] = []
        for item in items:
            memories = item.get("Memories") if isinstance(item, dict) else None
            if not isinstance(memories, list):
                continue
            for memory in memories:
                record = self._record_from_item(memory, user_id=user_id)
                if record is not None:
                    records.append(record)
        return records

    def _parse_list_memories_response(
        self, response: Any, *, user_id: str
    ) -> List[LongTermMemoryRecord]:
        """严格解析 ListMemories 响应：顶层 MemoryList[]（§7.4.1）。"""
        try:
            data = self._parse_json_response(response)
        except (json.JSONDecodeError, TypeError):
            logger.error("ListMemories: invalid JSON response")
            return []
        if not isinstance(data, dict):
            logger.error("ListMemories: unexpected payload type")
            return []
        items = data.get("MemoryList")
        if not isinstance(items, list):
            logger.warning(
                "ListMemories: unknown schema, keys=%s; fail closed",
                list(data.keys()),
            )
            return []
        records: List[LongTermMemoryRecord] = []
        for item in items:
            record = self._record_from_item(item, user_id=user_id)
            if record is not None:
                records.append(record)
        return records

    def _record_from_item(
        self, item: Any, *, user_id: str
    ) -> LongTermMemoryRecord | None:
        """将单个记忆条目解析为 LongTermMemoryRecord。

        缺少 MemoryId 或正文的条目返回 None（无法支撑 mutation，fail closed）。
        """
        if not isinstance(item, dict):
            return None
        memory_id = item.get("MemoryId")
        content = item.get("Memory")
        if not isinstance(memory_id, str) or not memory_id.strip():
            logger.warning("memory item without MemoryId skipped")
            return None
        if not isinstance(content, str) or not content.strip():
            logger.warning("memory item without content skipped")
            return None
        score = item.get("Score")
        parsed_score: float | None = None
        if isinstance(score, (int, float)):
            parsed_score = float(score)
        metadata: dict[str, Any] = {}
        for key in ("OccurredStart", "OccurredEnd"):
            value = item.get(key)
            if value is not None:
                metadata[key] = value
        agent_user_id = item.get("AgentUserId")
        if isinstance(agent_user_id, str) and agent_user_id:
            metadata["AgentUserId"] = agent_user_id
        return LongTermMemoryRecord(
            memory_id=memory_id.strip(),
            content=content.strip(),
            score=parsed_score,
            user_id=user_id,
            created_at=item.get("CreatedAt") if isinstance(item.get("CreatedAt"), str) else None,
            updated_at=item.get("UpdatedAt") if isinstance(item.get("UpdatedAt"), str) else None,
            metadata=metadata,
        )

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

        text = item.get("Content") or item.get("Text") or item.get("Memory") or item.get("Data")
        if text is None:
            return []
        if isinstance(text, (dict, list)):
            return [json.dumps(text, ensure_ascii=False)]
        return [str(text)]
