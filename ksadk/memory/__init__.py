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

from ksadk.memory.manager import MemoryManager, get_memory_manager
from ksadk.memory.backends.base import BaseMemoryBackend
from ksadk.memory.backends.memory import InMemoryBackend

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
    raise AttributeError(f"module 'ksadk.memory' has no attribute {name!r}")
