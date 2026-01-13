# ksadk/server/session_store.py
"""
内存 Session 存储 - 用于 ADK Web 前端集成
"""

import uuid
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class Message:
    """单条消息"""
    role: str
    parts: List[Dict[str, Any]]
    event_id: Optional[str] = None
    invocation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass  
class Session:
    """会话对象"""
    id: str
    app_name: str
    user_id: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "appName": self.app_name,
            "userId": self.user_id,
            "events": self.events,
            "state": self.state,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at
        }


class InMemorySessionStore:
    """内存 Session 存储"""
    
    _instance: Optional['InMemorySessionStore'] = None
    _lock = Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._sessions: Dict[str, Session] = {}
                    cls._instance._store_lock = Lock()
        return cls._instance
    
    def create_session(self, app_name: str, user_id: str, events: List[Dict] = None) -> Session:
        """创建新 Session"""
        session_id = str(uuid.uuid4())
        session = Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            events=events or []
        )
        with self._store_lock:
            self._sessions[session_id] = session
        return session
    
    def get_session(self, session_id: str) -> Optional[Session]:
        """获取 Session"""
        with self._store_lock:
            return self._sessions.get(session_id)
    
    def list_sessions(self, app_name: str, user_id: str) -> List[Session]:
        """列出用户的所有 Sessions"""
        with self._store_lock:
            return [
                s for s in self._sessions.values()
                if s.app_name == app_name and s.user_id == user_id
            ]
    
    def delete_session(self, session_id: str) -> bool:
        """删除 Session"""
        with self._store_lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False
    
    def add_event(self, session_id: str, event: Dict[str, Any]) -> bool:
        """向 Session 添加事件"""
        with self._store_lock:
            session = self._sessions.get(session_id)
            if session:
                session.events.append(event)
                session.updated_at = time.time()
                return True
            return False
    
    def update_state(self, session_id: str, state_delta: Dict[str, Any]) -> bool:
        """更新 Session 状态"""
        with self._store_lock:
            session = self._sessions.get(session_id)
            if session:
                session.state.update(state_delta)
                session.updated_at = time.time()
                return True
            return False


# 全局单例
def get_session_store() -> InMemorySessionStore:
    return InMemorySessionStore()
