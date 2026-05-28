"""InMemory 长期记忆后端 - 开发/测试用

使用简单的内存字典存储和文本匹配检索。
数据在进程退出后丢失，仅适用于开发和测试场景。
"""

import logging
from collections import defaultdict
from typing import List

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend

logger = logging.getLogger(__name__)


class InMemoryLTMBackend(BaseLongTermMemoryBackend):
    """内存长期记忆后端

    使用 dict 存储记忆，文本关键词匹配检索。
    适用于开发测试，不提供语义搜索能力。

    Examples:
        ```python
        backend = InMemoryLTMBackend(index="my_app")
        backend.save_memory("user_1", ["我喜欢吃冰淇淋", "今天天气真好"])
        results = backend.search_memory("user_1", "冰淇淋", top_k=5)
        ```
    """

    # Pydantic v2 不允许直接声明 mutable default，使用 model_post_init
    _storage: dict = None

    def model_post_init(self, __context) -> None:
        # {user_id: [event_string, ...]}
        self._storage = defaultdict(list)
        logger.info(
            f"InMemoryLTMBackend initialized: index={self.index}"
        )

    def save_memory(
        self, user_id: str, event_strings: List[str], **kwargs
    ) -> bool:
        """保存记忆到内存"""
        if not event_strings:
            return True

        self._storage[user_id].extend(event_strings)
        logger.debug(
            f"Saved {len(event_strings)} events for user={user_id}, "
            f"total={len(self._storage[user_id])}"
        )
        return True

    def search_memory(
        self, user_id: str, query: str, top_k: int = 5, **kwargs
    ) -> List[str]:
        """基于关键词匹配检索记忆

        简单实现：对 query 分词后，按匹配关键词数量排序。
        生产环境应使用向量搜索后端。
        """
        user_memories = self._storage.get(user_id, [])
        if not user_memories:
            return []

        # 简单的关键词匹配打分
        query_lower = query.lower()
        # 按字符分词（支持中英文混合）
        query_terms = query_lower.split()

        scored = []
        for memory in user_memories:
            memory_lower = memory.lower()
            # 计算匹配分数：完整 query 匹配得高分，部分关键词匹配得低分
            score = 0
            if query_lower in memory_lower:
                score += 10  # 完整匹配
            for term in query_terms:
                if term in memory_lower:
                    score += 1
            if score > 0:
                scored.append((score, memory))

        # 如果没有匹配，返回最近的记忆
        if not scored:
            return user_memories[-top_k:]

        # 按分数降序排列，取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item[1] for item in scored[:top_k]]

        logger.debug(
            f"Search memory for user={user_id} query='{query[:50]}': "
            f"found {len(results)} results from {len(user_memories)} total"
        )
        return results
