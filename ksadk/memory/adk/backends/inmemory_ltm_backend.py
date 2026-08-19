"""InMemory 长期记忆后端 - 开发/测试用

使用简单的内存字典存储和文本匹配检索。
数据在进程退出后丢失，仅适用于开发和测试场景。

扩展协议（技术改造方案 §7.3）：内存实现结构化检索与 update/delete，
用于本地开发和 fake 场景下的契约验证；不声明 flush/session_status 能力
（没有后台提取过程，写入即"可见"）。

兼容设计：_storage 保存原始事件字符串不改写，search_memory 返回原始
字符串列表（保留旧契约，§7.5）；结构化能力通过平行 ID 索引提供。
"""

import json
import logging
import uuid
from collections import defaultdict
from typing import List, Set

from pydantic import PrivateAttr

from ksadk.memory.adk.backends.base_ltm_backend import (
    CAP_ADD,
    CAP_DELETE,
    CAP_SEARCH,
    CAP_STRUCTURED_SEARCH,
    CAP_UPDATE,
    BaseLongTermMemoryBackend,
)
from ksadk.memory.models import (
    LongTermMemoryRecord,
    MemoryMutationResult,
)

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

    _storage: defaultdict[str, list[str]] = PrivateAttr(default_factory=lambda: defaultdict(list))
    # memory_id -> 原始事件字符串（不改写 _storage，保留旧 search_memory 契约）。
    _entry_ids: dict[str, str] = PrivateAttr(default_factory=dict)
    # (user_id) -> {memory_id -> 原始事件字符串}，用于快速定位与更新/删除。
    _user_entry_index: defaultdict[str, dict[str, str]] = PrivateAttr(
        default_factory=lambda: defaultdict(dict)
    )

    def model_post_init(self, __context) -> None:
        # {user_id: [event_string, ...]}
        logger.info(f"InMemoryLTMBackend initialized: index={self.index}")

    def save_memory(self, user_id: str, event_strings: List[str], **kwargs) -> bool:
        """保存记忆到内存"""
        if not event_strings:
            return True

        for entry in event_strings:
            memory_id = f"mem-{uuid.uuid4().hex[:12]}"
            self._entry_ids[memory_id] = entry
            self._user_entry_index[user_id][memory_id] = entry
            self._storage[user_id].append(entry)
        logger.debug(
            f"Saved {len(event_strings)} events for user={user_id}, "
            f"total={len(self._storage[user_id])}"
        )
        return True

    def search_memory(self, user_id: str, query: str, top_k: int = 5, **kwargs) -> List[str]:
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

        scored: list[tuple[int, str]] = []
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

    # ---- 扩展协议实现（§7.3） ----

    def capabilities(self) -> Set[str]:
        return {
            CAP_SEARCH,
            CAP_ADD,
            CAP_STRUCTURED_SEARCH,
            CAP_UPDATE,
            CAP_DELETE,
        }

    def search_records(
        self, user_id: str, query: str, top_k: int = 5, **kwargs
    ) -> List[LongTermMemoryRecord]:
        """结构化检索：复用关键词匹配，返回稳定生成的本地 ID。"""
        entries = self.search_memory(user_id, query, top_k=top_k)
        records: List[LongTermMemoryRecord] = []
        for entry in entries:
            memory_id = self._find_entry_id(user_id, entry)
            if memory_id is None:
                continue
            records.append(
                LongTermMemoryRecord(
                    memory_id=memory_id,
                    content=self._entry_text(entry),
                    score=None,
                    user_id=user_id,
                )
            )
        return records

    def update_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        **kwargs,
    ) -> MemoryMutationResult:
        user_index = self._user_entry_index.get(user_id, {})
        old_entry = user_index.get(memory_id)
        if old_entry is None:
            return MemoryMutationResult(ok=False, memory_id=memory_id, status="not_found")
        new_entry = json.dumps(
            {"role": "user", "parts": [{"text": content}]}, ensure_ascii=False
        )
        memories = self._storage[user_id]
        for i, entry in enumerate(memories):
            if entry is old_entry:
                memories[i] = new_entry
                break
        self._entry_ids[memory_id] = new_entry
        user_index[memory_id] = new_entry
        return MemoryMutationResult(
            ok=True,
            memory_id=memory_id,
            new_memory_id=memory_id,
            status="updated",
        )

    def delete_memory(
        self,
        *,
        user_id: str,
        memory_id: str,
        **kwargs,
    ) -> MemoryMutationResult:
        user_index = self._user_entry_index.get(user_id, {})
        entry = user_index.pop(memory_id, None)
        if entry is None:
            return MemoryMutationResult(
                ok=True, memory_id=memory_id, status="already_absent", message="目标记忆已不存在"
            )
        self._entry_ids.pop(memory_id, None)
        memories = self._storage.get(user_id, [])
        try:
            memories.remove(entry)
        except ValueError:
            pass
        return MemoryMutationResult(ok=True, memory_id=memory_id, status="deleted")

    def _find_entry_id(self, user_id: str, entry: str) -> str | None:
        """根据原始条目定位 memory_id（反向查表）。"""
        for memory_id, stored in self._user_entry_index.get(user_id, {}).items():
            if stored is entry or stored == entry:
                return memory_id
        return None

    # ---- 内部工具 ----

    @staticmethod
    def _entry_text(entry: str) -> str:
        """提取事件字符串里的正文（兼容 JSON 事件与纯文本）。"""
        try:
            payload = json.loads(entry)
        except (json.JSONDecodeError, TypeError):
            return entry
        if isinstance(payload, dict):
            parts = payload.get("parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], dict):
                return str(parts[0].get("text", entry))
        return entry
