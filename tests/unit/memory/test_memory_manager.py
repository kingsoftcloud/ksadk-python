"""Memory Manager SDK 单元测试"""

import pytest
from datetime import timedelta

from ksadk.memory import MemoryManager, InMemoryBackend


class TestInMemoryBackend:
    """InMemoryBackend 测试"""
    
    def test_set_and_get(self):
        backend = InMemoryBackend()
        backend.set("key1", "value1")
        assert backend.get("key1") == "value1"
    
    def test_get_nonexistent(self):
        backend = InMemoryBackend()
        assert backend.get("nonexistent") is None
    
    def test_session_isolation(self):
        backend = InMemoryBackend()
        backend.set("key", "value1", session_id="sess-1")
        backend.set("key", "value2", session_id="sess-2")
        
        assert backend.get("key", session_id="sess-1") == "value1"
        assert backend.get("key", session_id="sess-2") == "value2"
    
    def test_delete(self):
        backend = InMemoryBackend()
        backend.set("key", "value")
        assert backend.delete("key") == True
        assert backend.get("key") is None
    
    def test_exists(self):
        backend = InMemoryBackend()
        backend.set("key", "value")
        assert backend.exists("key") == True
        assert backend.exists("nonexistent") == False
    
    def test_clear_session(self):
        backend = InMemoryBackend()
        backend.set("key1", "value1", session_id="sess-1")
        backend.set("key2", "value2", session_id="sess-1")
        backend.add_message("sess-1", "user", "hello")
        
        backend.clear_session("sess-1")
        
        assert backend.get("key1", session_id="sess-1") is None
        assert backend.get("key2", session_id="sess-1") is None
        assert backend.get_messages("sess-1") == []
    
    def test_add_and_get_messages(self):
        backend = InMemoryBackend()
        backend.add_message("sess-1", "user", "你好")
        backend.add_message("sess-1", "assistant", "你好！")
        
        messages = backend.get_messages("sess-1")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "你好"
        assert messages[1]["role"] == "assistant"
    
    def test_get_messages_with_limit(self):
        backend = InMemoryBackend()
        for i in range(10):
            backend.add_message("sess-1", "user", f"message {i}")
        
        messages = backend.get_messages("sess-1", limit=3)
        assert len(messages) == 3
        assert messages[0]["content"] == "message 7"  # 最后 3 条


class TestMemoryManager:
    """MemoryManager 测试"""
    
    def test_create_with_in_memory(self):
        memory = MemoryManager(backend="memory")
        assert memory.backend_name == "memory"
    
    def test_set_and_get(self):
        memory = MemoryManager()
        memory.set("user", {"name": "Alice"}, session_id="sess-1")
        
        user = memory.get("user", session_id="sess-1")
        assert user == {"name": "Alice"}
    
    def test_state_operations(self):
        memory = MemoryManager()
        
        memory.set_state("sess-1", {"step": 1, "mode": "chat"})
        state = memory.get_state("sess-1")
        assert state == {"step": 1, "mode": "chat"}
        
        memory.update_state("sess-1", {"step": 2})
        state = memory.get_state("sess-1")
        assert state == {"step": 2, "mode": "chat"}
    
    def test_message_operations(self):
        memory = MemoryManager()
        
        memory.add_message("sess-1", "user", "Hello")
        memory.add_message("sess-1", "assistant", "Hi there!")
        
        messages = memory.get_messages("sess-1")
        assert len(messages) == 2
    
    def test_context_manager(self):
        with MemoryManager() as memory:
            memory.set("key", "value")
            assert memory.get("key") == "value"


class TestMemoryManagerFromEnv:
    """测试环境变量配置"""
    
    def test_default_backend(self, monkeypatch):
        # 不设置环境变量，应该使用 memory 后端
        monkeypatch.delenv("KSADK_MEMORY_BACKEND", raising=False)
        
        memory = MemoryManager.from_env()
        assert memory.backend_name == "memory"
    
    def test_with_ttl(self, monkeypatch):
        monkeypatch.setenv("KSADK_MEMORY_TTL", "3600")
        
        memory = MemoryManager.from_env()
        # TTL 设置成功不报错


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
