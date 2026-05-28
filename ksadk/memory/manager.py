"""Memory Manager - 统一记忆管理接口"""

import os
import logging
from datetime import timedelta
from typing import Any, Optional, List, Dict, Type

from ksadk.memory.backends.base import BaseMemoryBackend
from ksadk.memory.backends.memory import InMemoryBackend

logger = logging.getLogger(__name__)


# 后端注册表
_BACKENDS: Dict[str, Type[BaseMemoryBackend]] = {
    "memory": InMemoryBackend,
}


def register_backend(name: str, backend_class: Type[BaseMemoryBackend]):
    """注册自定义后端"""
    _BACKENDS[name] = backend_class


class MemoryManager:
    """统一记忆管理器
    
    支持可插拔后端，自动从环境变量读取配置。
    
    使用示例:
        # 从环境变量自动配置
        memory = MemoryManager.from_env()
        
        # 手动配置
        memory = MemoryManager(backend="redis", url="redis://localhost:6379")
        
        # 存取数据
        memory.set("key", {"data": "value"}, session_id="sess-123")
        data = memory.get("key", session_id="sess-123")
        
        # 消息历史
        memory.add_message("sess-123", "user", "你好")
        memory.add_message("sess-123", "assistant", "你好！有什么可以帮助你的？")
        messages = memory.get_messages("sess-123")
    """
    
    def __init__(
        self,
        backend: str = "memory",
        url: Optional[str] = None,
        prefix: str = "ksadk:memory:",
        default_ttl: Optional[timedelta] = None,
        **kwargs,
    ):
        """初始化 Memory Manager
        
        Args:
            backend: 后端类型 ("memory", "redis")
            url: 后端连接 URL
            prefix: 键名前缀
            default_ttl: 默认过期时间
            **kwargs: 传递给后端的额外参数
        """
        self.backend_name = backend
        self._backend = self._create_backend(backend, url, prefix, default_ttl, **kwargs)
    
    def _create_backend(
        self,
        backend: str,
        url: Optional[str],
        prefix: str,
        default_ttl: Optional[timedelta],
        **kwargs,
    ) -> BaseMemoryBackend:
        """创建后端实例"""
        # 延迟导入 Redis backend
        if backend == "redis":
            from ksadk.memory.backends.redis import RedisBackend
            _BACKENDS["redis"] = RedisBackend
        
        if backend not in _BACKENDS:
            raise ValueError(f"Unknown backend: {backend}. Available: {list(_BACKENDS.keys())}")
        
        backend_class = _BACKENDS[backend]
        
        # 根据后端类型传递参数
        if backend == "memory":
            return backend_class()
        elif backend == "redis":
            return backend_class(url=url, prefix=prefix, default_ttl=default_ttl, **kwargs)
        else:
            return backend_class(**kwargs)
    
    @classmethod
    def from_env(cls) -> "MemoryManager":
        """从环境变量创建 MemoryManager
        
        环境变量:
            KSADK_MEMORY_BACKEND: 后端类型 (默认 "memory")
            KSADK_MEMORY_URL: 连接 URL (如 redis://localhost:6379)
            KSADK_MEMORY_PREFIX: 键名前缀 (默认 "ksadk:memory:")
            KSADK_MEMORY_TTL: 默认 TTL 秒数 (可选)
        """
        backend = os.environ.get("KSADK_MEMORY_BACKEND", "memory")
        url = os.environ.get("KSADK_MEMORY_URL", "")
        prefix = os.environ.get("KSADK_MEMORY_PREFIX", "ksadk:memory:")
        
        ttl = None
        ttl_str = os.environ.get("KSADK_MEMORY_TTL", "")
        if ttl_str:
            ttl = timedelta(seconds=int(ttl_str))
        
        logger.debug(f"MemoryManager.from_env: backend={backend}, url={url[:20]}...")
        
        return cls(
            backend=backend,
            url=url or None,
            prefix=prefix,
            default_ttl=ttl,
        )
    
    # ===== 键值操作 =====
    
    def get(self, key: str, session_id: Optional[str] = None) -> Optional[Any]:
        """获取值"""
        return self._backend.get(key, session_id)
    
    def set(
        self,
        key: str,
        value: Any,
        session_id: Optional[str] = None,
        ttl: Optional[timedelta] = None,
    ) -> bool:
        """设置值"""
        return self._backend.set(key, value, session_id, ttl)
    
    def delete(self, key: str, session_id: Optional[str] = None) -> bool:
        """删除键"""
        return self._backend.delete(key, session_id)
    
    def exists(self, key: str, session_id: Optional[str] = None) -> bool:
        """检查键是否存在"""
        return self._backend.exists(key, session_id)
    
    # ===== 会话操作 =====
    
    def clear_session(self, session_id: str) -> bool:
        """清除会话所有数据"""
        return self._backend.clear_session(session_id)
    
    # ===== 消息历史 =====
    
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """添加消息"""
        return self._backend.add_message(session_id, role, content, metadata)
    
    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """获取消息历史"""
        return self._backend.get_messages(session_id, limit)
    
    # ===== 便捷方法 =====
    
    def get_state(self, session_id: str) -> Dict:
        """获取会话状态 (短期记忆)"""
        return self.get("__state__", session_id) or {}
    
    def set_state(self, session_id: str, state: Dict, ttl: Optional[timedelta] = None) -> bool:
        """设置会话状态"""
        return self.set("__state__", state, session_id, ttl)
    
    def update_state(self, session_id: str, updates: Dict) -> bool:
        """更新会话状态 (合并)"""
        current = self.get_state(session_id)
        current.update(updates)
        return self.set_state(session_id, current)
    
    def close(self) -> None:
        """关闭连接"""
        self._backend.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# 全局单例 (懒加载)
_global_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取全局 MemoryManager 单例
    
    在 Agent 代码中使用:
        from ksadk.memory import get_memory_manager
        
        memory = get_memory_manager()
        memory.set("key", "value")
    """
    global _global_manager
    if _global_manager is None:
        _global_manager = MemoryManager.from_env()
    return _global_manager
