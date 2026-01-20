"""Memory Manager SDK - 可插拔记忆管理

支持 Agent 自选存储后端管理长短期记忆。

使用示例:
    from ksadk.memory import MemoryManager, get_memory_manager

    # 自动从环境变量读取配置
    memory = MemoryManager.from_env()

    # 或使用全局单例
    memory = get_memory_manager()

    # 使用
    memory.set("user_preference", {"theme": "dark"})
    pref = memory.get("user_preference")
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
