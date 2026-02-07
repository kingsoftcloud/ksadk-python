"""Redis Backend - Redis 存储 (短期记忆)"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict

from ksadk.memory.backends.base import BaseMemoryBackend


class RedisBackend(BaseMemoryBackend):
    """Redis 存储后端
    
    适用于短期记忆和会话状态存储。
    
    使用示例:
        backend = RedisBackend(url="redis://localhost:6379/0")
    """
    
    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        prefix: str = "ksadk:memory:",
        default_ttl: Optional[timedelta] = None,
    ):
        self.url = url
        self.prefix = prefix
        self.default_ttl = default_ttl
        self._client = None
    
    @property
    def client(self):
        """懒加载 Redis 客户端"""
        if self._client is None:
            try:
                import redis
                self._client = redis.from_url(self.url, decode_responses=True)
            except ImportError:
                raise ImportError("redis package required. Install with: pip install redis")
        return self._client
    
    def _make_key(self, key: str, session_id: Optional[str]) -> str:
        """生成 Redis 键"""
        if session_id:
            return f"{self.prefix}session:{session_id}:{key}"
        return f"{self.prefix}global:{key}"
    
    def _messages_key(self, session_id: str) -> str:
        return f"{self.prefix}messages:{session_id}"
    
    def get(self, key: str, session_id: Optional[str] = None) -> Optional[Any]:
        redis_key = self._make_key(key, session_id)
        value = self.client.get(redis_key)
        
        if value is None:
            return None
        
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    
    def set(
        self,
        key: str,
        value: Any,
        session_id: Optional[str] = None,
        ttl: Optional[timedelta] = None,
    ) -> bool:
        redis_key = self._make_key(key, session_id)
        
        # 序列化
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        
        # 设置过期时间
        ex = None
        if ttl:
            ex = int(ttl.total_seconds())
        elif self.default_ttl:
            ex = int(self.default_ttl.total_seconds())
        
        self.client.set(redis_key, value, ex=ex)
        return True
    
    def delete(self, key: str, session_id: Optional[str] = None) -> bool:
        redis_key = self._make_key(key, session_id)
        return self.client.delete(redis_key) > 0
    
    def exists(self, key: str, session_id: Optional[str] = None) -> bool:
        redis_key = self._make_key(key, session_id)
        return self.client.exists(redis_key) > 0
    
    def clear_session(self, session_id: str) -> bool:
        pattern = f"{self.prefix}session:{session_id}:*"
        keys = list(self.client.scan_iter(match=pattern))
        
        # 也删除消息
        keys.append(self._messages_key(session_id))
        
        if keys:
            self.client.delete(*keys)
        return True
    
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict] = None,
    ) -> bool:
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if metadata:
            msg["metadata"] = metadata
        
        redis_key = self._messages_key(session_id)
        self.client.rpush(redis_key, json.dumps(msg, ensure_ascii=False))
        
        # 可以设置消息历史的过期时间
        if self.default_ttl:
            self.client.expire(redis_key, int(self.default_ttl.total_seconds()))
        
        return True
    
    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        redis_key = self._messages_key(session_id)
        
        if limit:
            # 获取最后 N 条
            raw = self.client.lrange(redis_key, -limit, -1)
        else:
            raw = self.client.lrange(redis_key, 0, -1)
        
        return [json.loads(m) for m in raw]
    
    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
