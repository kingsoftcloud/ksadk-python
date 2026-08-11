"""SQLite 长期记忆后端 — 持久化 LTM（适配 BaseLongTermMemoryBackend 接口）。

用 ``SqliteMemoryProvider`` 的持久 SQLite 路径，解决 ``InMemoryLTMBackend`` 进程退出后
数据丢失、每次新实例数据不延续的问题。recall 和 flush 共用同一 SQLite 文件。
"""

from __future__ import annotations

import json
import logging
from typing import List

from pydantic import PrivateAttr

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend

logger = logging.getLogger(__name__)


class SqliteLTMBackend(BaseLongTermMemoryBackend):
    """SQLite 持久长期记忆后端。

    使用 ``SqliteMemoryProvider`` 的持久化路径（``KSADK_MEMORY_DB_PATH`` 或本地 session dir），
    通过 ``MemoryCoordinator`` 做检索。recall 和 flush 共用同一文件，数据跨进程延续。

    适配 ``BaseLongTermMemoryBackend`` 接口（save_memory/search_memory），供
    ``LongTermMemoryService`` 的 "local" backend 使用。
    """

    _provider: object = PrivateAttr(default=None)

    def model_post_init(self, __context) -> None:
        from ksadk.memory.providers.local_sqlite import (
            SqliteMemoryProvider,
            _resolve_default_db_path,
        )

        path = _resolve_default_db_path()
        self._provider = SqliteMemoryProvider(db_path=path, tenant_id="local", workspace_id="local")
        logger.info("SqliteLTMBackend initialized: index=%s, db=%s", self.index, path)

    def save_memory(self, user_id: str, event_strings: List[str], **kwargs) -> bool:
        """保存记忆到持久 SQLite。"""
        if not event_strings:
            return True
        for event_str in event_strings:
            try:
                payload = json.loads(event_str)
                content = str(payload.get("parts", [{}])[0].get("text", "") or event_str)
            except (json.JSONDecodeError, TypeError, IndexError):
                content = event_str
            from ksadk.memory.models import MemoryCandidate

            candidate = MemoryCandidate(
                candidate_id=f"ltm_{abs(hash(content)) % 10**16}",
                operation="add",
                memory_type="profile",
                scope="user",
                scope_id=user_id,
                content=content,
                confidence=0.9,
                importance=0.7,
                source_event_ids=[],
                reason="explicit_user_request",
            )
            from ksadk.memory.coordinator import MemoryCoordinator

            coordinator = MemoryCoordinator(self._provider)
            coordinator.flush_candidates([candidate])
        return True

    def search_memory(self, user_id: str, query: str, top_k: int = 5, **kwargs) -> List[str]:
        """从持久 SQLite 检索记忆。"""
        from ksadk.memory.coordinator import MemoryCoordinator, build_search_request

        coordinator = MemoryCoordinator(self._provider)
        request = build_search_request(query=query, user_id=user_id, top_k=top_k)
        result = coordinator.recall(request)
        if result.status != "ok":
            return []
        entries: list[str] = []
        for record in result.records:
            entries.append(
                json.dumps(
                    {
                        "parts": [{"text": record.content}],
                        "metadata": {"memory_id": record.memory_id},
                    },
                    ensure_ascii=False,
                )
            )
        return entries


__all__ = ["SqliteLTMBackend"]
