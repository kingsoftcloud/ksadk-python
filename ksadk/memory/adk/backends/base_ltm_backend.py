"""长期记忆后端抽象基类

所有长期记忆后端必须继承此类并实现 save_memory / search_memory 方法。

参考 VeADK: veadk/memory/long_term_memory_backends/base_backend.py
"""

from abc import ABC, abstractmethod
from typing import List

from pydantic import BaseModel


class BaseLongTermMemoryBackend(ABC, BaseModel):
    """长期记忆存储后端抽象基类

    Attributes:
        index: 索引/集合名称，用于隔离不同应用的记忆数据
        last_error: 最近一次 search/save 失败的原因。成功调用前置空，失败时填充。
            上层（LongTermMemoryService.build_context）据此区分"后端吞错返空"与
            "真无记忆"——前者不得把错误伪装成"未找到"注入模型上下文。
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
