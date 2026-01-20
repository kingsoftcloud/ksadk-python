"""Base Memory Backend - 抽象基类"""

from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict
from datetime import timedelta


class BaseMemoryBackend(ABC):
    """记忆存储后端抽象基类
    
    所有后端实现必须继承此类并实现以下方法。
    """
    
    @abstractmethod
    def get(self, key: str, session_id: Optional[str] = None) -> Optional[Any]:
        """获取值
        
        Args:
            key: 键名
            session_id: 可选的会话 ID，用于隔离不同会话的数据
            
        Returns:
            存储的值，不存在返回 None
        """
        pass
    
    @abstractmethod
    def set(
        self,
        key: str,
        value: Any,
        session_id: Optional[str] = None,
        ttl: Optional[timedelta] = None,
    ) -> bool:
        """设置值
        
        Args:
            key: 键名
            value: 值 (会自动序列化)
            session_id: 可选的会话 ID
            ttl: 可选的过期时间
            
        Returns:
            是否成功
        """
        pass
    
    @abstractmethod
    def delete(self, key: str, session_id: Optional[str] = None) -> bool:
        """删除键
        
        Args:
            key: 键名
            session_id: 可选的会话 ID
            
        Returns:
            是否成功
        """
        pass
    
    @abstractmethod
    def exists(self, key: str, session_id: Optional[str] = None) -> bool:
        """检查键是否存在"""
        pass
    
    @abstractmethod
    def clear_session(self, session_id: str) -> bool:
        """清除整个会话的所有数据"""
        pass
    
    # ===== 消息历史相关 (短期记忆) =====
    
    @abstractmethod
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """添加消息到会话历史"""
        pass
    
    @abstractmethod
    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        """获取会话消息历史
        
        Args:
            session_id: 会话 ID
            limit: 最近 N 条消息，None 表示全部
            
        Returns:
            消息列表 [{role, content, timestamp, ...}]
        """
        pass
    
    def close(self) -> None:
        """关闭连接 (可选实现)"""
        pass
