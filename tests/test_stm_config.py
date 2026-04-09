from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.adk_runner import ADKRunner
from ksadk.sessions import create_session_service
from ksadk.sessions.local_service import LocalSessionService


def _make_adk_runner() -> ADKRunner:
    detection = SimpleNamespace(entry_point="agent.py", agent_variable="root_agent")
    return ADKRunner(detection, "/tmp/test-project")


def test_platform_session_service_prefers_ksadk_stm_path(monkeypatch, tmp_path):
    target = tmp_path / "shared-sessions.sqlite"
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_STM_PATH", str(target))

    service = create_session_service()

    assert isinstance(service, LocalSessionService)
    assert service.db_path == target.resolve()


def test_platform_session_service_keeps_legacy_stm_db_path_alias(monkeypatch, tmp_path):
    target = tmp_path / "legacy-sessions.sqlite"
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("AGENTENGINE_UI_DIR", raising=False)
    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.delenv("KSADK_STM_PATH", raising=False)
    monkeypatch.setenv("KSADK_STM_DB_PATH", str(target))

    service = create_session_service()

    assert isinstance(service, LocalSessionService)
    assert service.db_path == target.resolve()


def test_short_term_memory_from_env_prefers_stm_path_alias(monkeypatch):
    from ksadk.memory.adk.short_term_memory import ShortTermMemory

    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/shared-sessions.sqlite")
    monkeypatch.delenv("KSADK_STM_DB_PATH", raising=False)
    monkeypatch.delenv("KSADK_ADK_SESSION_PATH", raising=False)

    stm = ShortTermMemory.from_env()

    assert stm.backend == "sqlite"
    assert stm.local_database_path == "/tmp/shared-sessions.sqlite"


def test_short_term_memory_from_env_prefers_adk_session_override(monkeypatch):
    from ksadk.memory.adk.short_term_memory import ShortTermMemory

    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/shared-sessions.sqlite")
    monkeypatch.setenv("KSADK_ADK_SESSION_PATH", "/tmp/adk-private.sqlite")

    stm = ShortTermMemory.from_env()

    assert stm.local_database_path == "/tmp/adk-private.sqlite"


def test_adk_runner_short_term_memory_uses_framework_specific_override(monkeypatch):
    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/shared-sessions.sqlite")
    monkeypatch.setenv("KSADK_ADK_SESSION_PATH", "/tmp/adk-private.sqlite")
    runner = _make_adk_runner()

    stm = runner._init_short_term_memory()

    assert stm is not None
    assert stm.local_database_path == "/tmp/adk-private.sqlite"
