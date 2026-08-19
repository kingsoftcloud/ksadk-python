"""长期记忆后端抽象基类

所有长期记忆后端必须继承此类并实现 save_memory / search_memory 方法。

参考 VeADK: veadk/memory/long_term_memory_backends/base_backend.py

扩展协议（技术改造方案 §7.3）：
    - search_records: 结构化检索，返回带 memory_id 的 LongTermMemoryRecord
    - update_memory / delete_memory: 按 ID 原地更新/软删除
    - capabilities: 声明 backend 支持的能力集合
    基类对扩展方法提供默认实现：抛出 UnsupportedMemoryOperation 并在
    capabilities 中不声明对应能力，旧 backend 无需改动即保持兼容。
"""

from abc import ABC, abstractmethod
from typing import List, Set

from pydantic import BaseModel

from ksadk.memory.models import (
    LongTermMemoryRecord,
    MemoryExtractionStatus,
    MemoryMutationResult,
    UnsupportedMemoryOperation,
)

# 能力常量（§7.3）
CAP_SEARCH = "search"
CAP_ADD = "add"
CAP_FLUSH = "flush"
CAP_STRUCTURED_SEARCH = "structured_search"
CAP_UPDATE = "update"
CAP_DELETE = "delete"
CAP_SESSION_STATUS = "session_status"


class BaseLongTermMemoryBackend(ABC, BaseModel):
    """长期记忆存储后端抽象基类

    Attributes:
        index: 索引/集合名称，用于隔离不同应用的记忆数据
        last_error: 最近一次 search/save 失败的原因。成功调用前置空，失败时填充。
            上层（LongTermMemoryService.build_context）据此区分"后端吞错返空"与"真无记忆"。
    """

    index: str = ""
    last_error: str = ""

    @abstractmethod
    def save_memory(self, user_id: str, event_strings: List[str], **kwargs) -> bool:
        """保存记忆

        Args:
            user_id: 用户 ID
            event_strings: 序列化的事件字符串列表

        Returns:
            是否保存成功
        """
        pass

    @abstractmethod
    def search_memory(self, user_id: str, query: str, top_k: int = 5, **kwargs) -> List[str]:
        """检索记忆

        Args:
            user_id: 用户 ID
            query: 查询文本
            top_k: 返回最相关的 N 条记忆

        Returns:
            匹配的记忆字符串列表
        """
        pass

    # ---- 扩展协议（可选能力，默认 unsupported） ----

    def search_records(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        **kwargs,
    ) -> List[LongTermMemoryRecord]:
        """结构化检索：返回带服务端 memory_id 的记录列表。

        不支持的 backend 抛出 UnsupportedMemoryOperation，
        不得降级为返回正文 hash 伪造的 ID。
        """
        raise UnsupportedMemoryOperation(
            f"{type(self).__name__} does not support structured search"
        )

    def update_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        **kwargs,
    ) -> MemoryMutationResult:
        """按 memory_id 原地更新记忆正文。

        不支持的 backend 抛出 UnsupportedMemoryOperation，
        不得静默追加一条新记忆来模拟 update。
        """
        raise UnsupportedMemoryOperation(f"{type(self).__name__} does not support update")

    def delete_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        **kwargs,
    ) -> MemoryMutationResult:
        """按 memory_id 软删除指定记忆。

        不支持的 backend 抛出 UnsupportedMemoryOperation。
        """
        raise UnsupportedMemoryOperation(f"{type(self).__name__} does not support delete")

    def get_extraction_status(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> MemoryExtractionStatus:
        """查询 Session 后台提取状态（写后确认，§7.7）。

        不支持的 backend 抛出 UnsupportedMemoryOperation。
        """
        raise UnsupportedMemoryOperation(f"{type(self).__name__} does not support session status")

    def capabilities(self) -> Set[str]:
        """声明本 backend 支持的能力集合。

        基类默认只声明基础读写能力；子类按真实实现覆写，
        声明必须与实际可用方法一致（§11.1）。
        """
        return {CAP_SEARCH, CAP_ADD}

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities()
