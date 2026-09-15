from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.adk_runner import ADKRunner
from ksadk.sessions import bind_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.in_memory import InMemorySessionService


class _AdkSessionService:
    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str, str], SimpleNamespace] = {}
        self.creates: list[tuple[str, str, str]] = []

    async def get_session(self, *, app_name: str, user_id: str, session_id: str):
        return self.sessions.get((app_name, user_id, session_id))

    async def create_session(self, *, app_name: str, user_id: str, session_id: str | None = None):
        resolved_id = session_id or f"generated-{len(self.sessions) + 1}"
        session = SimpleNamespace(id=resolved_id)
        self.sessions[(app_name, user_id, resolved_id)] = session
        self.creates.append((app_name, user_id, resolved_id))
        return session


def _runner(tmp_path) -> ADKRunner:
    detection = SimpleNamespace(name="agent-1", entry_point="agent.py", agent_variable="root")
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="agent-1")
    runner._session_service = _AdkSessionService()
    return runner


@pytest.mark.asyncio
async def test_adk_native_session_uses_identity_user_and_persists_binding(
    tmp_path, monkeypatch
) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_invocation_session_owner", lambda: ("aeu-owner-a", "aei-owner-a"))

    with bind_session_service(canonical):
        internal_id = await runner._ensure_session("session-1")
        binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
            "session-1", "adk"
        )

    assert internal_id == "session-1"
    assert runner._session_service.creates == [("agent-1", "aeu-owner-a", "session-1")]
    assert binding["native_user_id"] == "aeu-owner-a"
    assert binding["owner_scope_ref"] == "aei-owner-a"


@pytest.mark.asyncio
async def test_adk_binding_rejects_a_different_identity_scope(tmp_path, monkeypatch) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_invocation_session_owner", lambda: ("aeu-owner-a", "aei-owner-a"))
    with bind_session_service(canonical):
        await runner._ensure_session("session-1")

    monkeypatch.setattr(runner, "_invocation_session_owner", lambda: ("aeu-owner-b", "aei-owner-b"))
    with (
        bind_session_service(canonical),
        pytest.raises(ValueError, match="different business identity"),
    ):
        await runner._ensure_session("session-1")


@pytest.mark.asyncio
async def test_adk_authorized_legacy_session_keeps_ksadk_user_history(
    tmp_path, monkeypatch
) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "legacy-user", "legacy-session")
    runner = _runner(tmp_path)
    runner._session_service.sessions[("agent-1", "ksadk_user", "legacy-session")] = SimpleNamespace(
        id="legacy-session"
    )
    monkeypatch.setattr(runner, "_invocation_session_owner", lambda: ("legacy-user", "aei-owner-a"))
    monkeypatch.setattr(runner, "_invocation_uses_legacy_session", lambda _user_id: True)

    with bind_session_service(canonical):
        internal_id = await runner._ensure_session("legacy-session")
        binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
            "legacy-session", "adk"
        )

    assert internal_id == "legacy-session"
    assert binding["native_user_id"] == "ksadk_user"
    assert runner._native_user_for_session("legacy-session") == "ksadk_user"


@pytest.mark.asyncio
async def test_new_identity_session_does_not_adopt_colliding_legacy_adk_state(
    tmp_path, monkeypatch
) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    runner._session_service.sessions[("agent-1", "ksadk_user", "session-1")] = SimpleNamespace(
        id="session-1"
    )
    monkeypatch.setattr(runner, "_invocation_session_owner", lambda: ("aeu-owner-a", "aei-owner-a"))
    monkeypatch.setattr(runner, "_invocation_uses_legacy_session", lambda _user_id: False)

    with bind_session_service(canonical):
        await runner._ensure_session("session-1")
        binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
            "session-1", "adk"
        )

    assert runner._session_service.creates == [("agent-1", "aeu-owner-a", "session-1")]
    assert binding["native_user_id"] == "aeu-owner-a"
