"""In-Memory Backend - 内存存储 (开发/测试用)"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict
from collections import defaultdict

from ksadk.memory.backends.base import BaseMemoryBackend


class InMemoryBackend(BaseMemoryBackend):
    """内存存储后端
    
    适用于开发和测试，数据在进程退出后丢失。
    """
    
    def __init__(self):
        # {session_id: {key: (value, expires_at)}}
        self._data: Dict[str, Dict[str, tuple]] = defaultdict(dict)
        # {session_id: [messages]}
        self._messages: Dict[str, List[Dict]] = defaultdict(list)
    
    def _make_key(self, key: str, session_id: Optional[str]) -> tuple:
        """生成复合键"""
        sid = session_id or "__global__"
        return sid, key
    
    def _is_expired(self, expires_at: Optional[datetime]) -> bool:
        if expires_at is None:
            return False
        return datetime.utcnow() > expires_at
    
    def get(self, key: str, session_id: Optional[str] = None) -> Optional[Any]:
        sid, k = self._make_key(key, session_id)
        
        if sid not in self._data or k not in self._data[sid]:
            return None
        
        value, expires_at = self._data[sid][k]
        
        if self._is_expired(expires_at):
            del self._data[sid][k]
            return None
        
        return value
    
    def set(
        self,
        key: str,
        value: Any,
        session_id: Optional[str] = None,
        ttl: Optional[timedelta] = None,
    ) -> bool:
        sid, k = self._make_key(key, session_id)
        
        expires_at = None
        if ttl:
            expires_at = datetime.utcnow() + ttl
        
        self._data[sid][k] = (value, expires_at)
        return True
    
    def delete(self, key: str, session_id: Optional[str] = None) -> bool:
        sid, k = self._make_key(key, session_id)
        
        if sid in self._data and k in self._data[sid]:
            del self._data[sid][k]
            return True
        return False
    
    def exists(self, key: str, session_id: Optional[str] = None) -> bool:
        return self.get(key, session_id) is not None
    
    def clear_session(self, session_id: str) -> bool:
        if session_id in self._data:
            del self._data[session_id]
        if session_id in self._messages:
            del self._messages[session_id]
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
        
        self._messages[session_id].append(msg)
        return True
    
    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict]:
        messages = self._messages.get(session_id, [])
        
        if limit:
            return messages[-limit:]
        return messages.copy()
    
    def close(self) -> None:
        self._data.clear()
        self._messages.clear()
