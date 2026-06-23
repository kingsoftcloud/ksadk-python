from __future__ import annotations

from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope


class _FakeMemoryService:
    def __init__(self):
        self.search_calls: list[tuple[str, str, int | None]] = []
        self.save_calls: list[tuple[str, str, dict]] = []
        self._backend = None

    def search_text(self, *, user_id: str, query: str, top_k: int | None = None) -> str:
        self.search_calls.append((user_id, query, top_k))
        return f"memories for {user_id}: {query}"

    def save_text(self, *, user_id: str, content: str, metadata: dict) -> bool:
        self.save_calls.append((user_id, content, metadata))
        return True


class _AcceptedButUnverifiedMemoryService(_FakeMemoryService):
    def __init__(self):
        super().__init__()
        class SdkLTMBackend:
            last_error = ""

            def get_session_status(self, *, user_id: str, session_id: str) -> dict:
                return {"SessionId": session_id, "State": 0}

        self._backend = SdkLTMBackend()

    def search_entries(self, *, user_id: str, query: str, top_k: int | None = None) -> list[str]:
        self.search_calls.append((user_id, query, top_k))
        return []


class _FailingMemoryService(_FakeMemoryService):
    def __init__(self):
        super().__init__()
        self._backend = type("Backend", (), {"last_error": "NotFound: missing memory"})()

    def save_text(self, *, user_id: str, content: str, metadata: dict) -> bool:
        self.save_calls.append((user_id, content, metadata))
        return False


def _context() -> PlatformInvocationContext:
    return PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        history=[{"role": "user", "content": "hello"}],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="langgraph",
    )


def test_load_memory_uses_platform_invocation_context(monkeypatch):
    from ksadk.memory.tool import load_memory

    service = _FakeMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = load_memory("project status")

    assert result == "memories for user-1: project status"
    assert service.search_calls == [("user-1", "project status", None)]


def test_save_memory_persists_agent_and_session_metadata(monkeypatch):
    from ksadk.memory.tool import save_memory

    service = _FakeMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = save_memory("用户喜欢云主机")

    assert result == {"ok": True, "status": "persisted", "message": "记忆已保存。"}
    assert service.save_calls == [
        (
            "user-1",
            "用户喜欢云主机",
            {
                "agent_id": "demo-agent",
                "session_id": "sess-1",
                "runner_type": "langgraph",
            },
        )
    ]


def test_save_memory_without_runtime_context_returns_diagnostic(monkeypatch):
    from ksadk.memory.tool import save_memory

    service = _FakeMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    result = save_memory("no context")

    assert result["ok"] is False
    assert "缺少运行时上下文" in result["message"]
    assert service.save_calls == []


def test_save_memory_failure_includes_backend_error(monkeypatch):
    from ksadk.memory.tool import save_memory

    service = _FailingMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = save_memory("用户喜欢云主机")

    assert result["ok"] is False
    assert "记忆保存失败" in result["message"]
    assert "NotFound: missing memory" in result["message"]


def test_save_memory_reports_unverified_sdk_acceptance(monkeypatch):
    from ksadk.memory.tool import save_memory

    service = _AcceptedButUnverifiedMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = save_memory("用户喜欢云主机")

    assert result["ok"] is False
    assert result["status"] == "accepted_not_extracted"
    assert "尚未抽取" in result["message"]
    assert result["session_id"] == "sess-1"
    assert result["session_state"] == 0
    assert service.search_calls == [("user-1", "用户喜欢云主机", 1)]
