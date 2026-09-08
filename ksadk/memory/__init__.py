"""Memory SDK.

模块组成:
    - ksadk.memory.manager: KV 存储 + 消息历史
    - ksadk.memory.adk: ADK 专用 STM/LTM 集成
    - ksadk.memory.service: 平台级长期记忆 service
    - ksadk.memory.tool: 跨框架 load/save memory 工具

使用示例:
    from ksadk.memory import load_memory, save_memory
    from ksadk.memory import LongTermMemoryService
"""

from typing import TYPE_CHECKING

from ksadk.memory.backends.base import BaseMemoryBackend
from ksadk.memory.backends.memory import InMemoryBackend
from ksadk.memory.manager import MemoryManager, get_memory_manager

if TYPE_CHECKING:
    from ksadk.memory.service import LongTermMemoryService

__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "BaseMemoryBackend",
    "InMemoryBackend",
    "LongTermMemoryService",
    "load_memory",
    "save_memory",
    "create_langchain_tools",
    "create_adk_tool",
]


def create_langchain_tools():
    from ksadk.memory.langchain_tool import create_langchain_tools as _create

    return _create()


def create_adk_tool():
    from ksadk.memory.adk_tool import create_adk_tool as _create

    return _create()


def __getattr__(name):
    if name in {"load_memory", "save_memory"}:
        from ksadk.memory.langchain_tool import load_memory, save_memory

        return {"load_memory": load_memory, "save_memory": save_memory}[name]
    if name == "LongTermMemoryService":
        from ksadk.memory.service import LongTermMemoryService

        return LongTermMemoryService
    # Memory v2（方案 §10）：lazy import，避免在仅用旧 API 时强制加载 SQLite/tiktoken 依赖。
    _v2_names = {
        "MemoryRecord",
        "MemoryCandidate",
        "MemorySearchRequest",
        "MemorySearchResult",
        "MemoryCapabilities",
        "CoreMemoryBlock",
        "MemoryDeleteRequest",
        "MemoryDeleteResult",
        "MemoryProvider",
        "MemoryPolicy",
        "MemoryCoordinator",
        "MemoryExtractor",
        "SqliteMemoryProvider",
        "build_search_request",
        "recall_to_context_item",
        "propose_memory_candidates",
    }
    if name in _v2_names:
        if name in {
            "MemoryRecord",
            "MemoryCandidate",
            "MemorySearchRequest",
            "MemorySearchResult",
            "MemoryCapabilities",
            "CoreMemoryBlock",
            "MemoryDeleteRequest",
            "MemoryDeleteResult",
        }:
            from ksadk.memory import models as _models

            return getattr(_models, name)
        if name == "MemoryProvider":
            from ksadk.memory.provider import MemoryProvider

            return MemoryProvider
        if name == "MemoryPolicy":
            from ksadk.memory.policy import MemoryPolicy

            return MemoryPolicy
        if name == "MemoryCoordinator":
            from ksadk.memory.coordinator import MemoryCoordinator

            return MemoryCoordinator
        if name in {"build_search_request", "recall_to_context_item"}:
            from ksadk.memory import coordinator as _coord

            return getattr(_coord, name)
        if name == "MemoryExtractor":
            from ksadk.memory.extraction import MemoryExtractor

            return MemoryExtractor
        if name == "propose_memory_candidates":
            from ksadk.memory.extraction import propose_memory_candidates

            return propose_memory_candidates
        if name == "SqliteMemoryProvider":
            from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider

            return SqliteMemoryProvider
    raise AttributeError(f"module 'ksadk.memory' has no attribute {name!r}")
