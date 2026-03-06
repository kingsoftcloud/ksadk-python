"""Memory Manager SDK - 可插拔记忆管理

支持 Agent 自选存储后端管理长短期记忆。

模块组成:
    - ksadk.memory.manager: KV 存储 + 消息历史 (原有)
    - ksadk.memory.adk: ADK 记忆体集成 (ShortTermMemory + LongTermMemory)

使用示例:
    # KV 存储 (原有)
    from ksadk.memory import MemoryManager, get_memory_manager
    memory = get_memory_manager()

    # ADK 记忆体 (新增)
    from ksadk.memory.adk import ShortTermMemory, LongTermMemory
    stm = ShortTermMemory(backend="local")
    ltm = LongTermMemory(backend="local", app_name="my_app")
"""

from ksadk.memory.manager import MemoryManager, get_memory_manager
from ksadk.memory.backends.base import BaseMemoryBackend
from ksadk.memory.backends.memory import InMemoryBackend

__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "BaseMemoryBackend",
    "InMemoryBackend",
]
