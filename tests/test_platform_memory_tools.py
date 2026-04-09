from __future__ import annotations

from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope


class _FakeMemoryService:
    def __init__(self):
        self.search_calls: list[tuple[str, str, int | None]] = []
        self.save_calls: list[tuple[str, str, dict]] = []

    def search_text(self, *, user_id: str, query: str, top_k: int | None = None) -> str:
        self.search_calls.append((user_id, query, top_k))
        return f"memories for {user_id}: {query}"

    def save_text(self, *, user_id: str, content: str, metadata: dict) -> bool:
        self.save_calls.append((user_id, content, metadata))
        return True


def _context() -> PlatformInvocationContext:
    return PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        history=[{"role": "user", "content": "hello"}],
        input_parts=[],
        attachments=[],
        attachment_results=[],
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

    assert result == "记忆已保存。"
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

    assert "缺少运行时上下文" in result
    assert service.save_calls == []
