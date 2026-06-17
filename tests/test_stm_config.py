from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.adk_runner import ADKRunner
from ksadk.sessions import create_session_service, describe_session_backend, register_session_backend
from ksadk.sessions.in_memory import InMemorySessionService
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


def test_platform_session_service_supports_memory_backend(monkeypatch):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")

    service = create_session_service()

    assert isinstance(service, InMemorySessionService)


def test_platform_session_service_treats_local_as_sqlite(monkeypatch, tmp_path):
    target = tmp_path / "sessions.sqlite"
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv("KSADK_SESSION_PATH", str(target))

    service = create_session_service()

    assert isinstance(service, LocalSessionService)
    assert service.db_path == target.resolve()


def test_platform_session_service_accepts_sqlite_alias(monkeypatch, tmp_path):
    target = tmp_path / "sessions.sqlite"
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_SESSION_PATH", str(target))

    service = create_session_service()

    assert isinstance(service, LocalSessionService)
    assert service.db_path == target.resolve()


def test_platform_session_service_requires_postgres_dsn(monkeypatch):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.delenv("KSADK_SESSION_DSN", raising=False)
    monkeypatch.delenv("KSADK_STM_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)

    with pytest.raises(ValueError, match="KSADK_SESSION_DSN"):
        create_session_service()


def test_describe_session_backend_marks_postgres_as_shared(monkeypatch):
    dsn = "".join(
        [
            "postgresql://",
            "user",
            ":",
            "pass",
            "@",
            "example.invalid:5432/example_db",
        ]
    )
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", dsn)

    payload = describe_session_backend()

    assert payload["Backend"] == "postgres"
    assert payload["Shared"] is True
    assert payload["ProductionSafe"] is True
    assert payload["ContinuityDefault"] == "semantic/replay"
    assert "Dsn" not in payload
    assert "Namespace" not in payload


def test_describe_session_backend_marks_local_as_not_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv("KSADK_SESSION_PATH", str(tmp_path / "sessions.sqlite"))

    payload = describe_session_backend()

    assert payload["Backend"] == "local"
    assert payload["Shared"] is False
    assert payload["ProductionSafe"] is False
    assert payload["ContinuityDefault"] == "local_only"


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


def test_short_term_memory_from_env_falls_back_to_unified_session_dsn(monkeypatch):
    from ksadk.memory.adk.short_term_memory import ShortTermMemory

    dsn = "postgresql+asyncpg://user:pass@example.invalid:5432/session_db"
    monkeypatch.delenv("KSADK_ADK_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_ADK_SESSION_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_STM_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", dsn)

    stm = ShortTermMemory.from_env()

    assert stm.backend == "database"
    assert stm.db_url == dsn


def test_adk_runner_short_term_memory_initializes_from_unified_session_env(monkeypatch):
    dsn = "postgresql+asyncpg://user:pass@example.invalid:5432/session_db"
    monkeypatch.delenv("KSADK_ADK_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_ADK_SESSION_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_STM_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", dsn)
    runner = _make_adk_runner()

    stm = runner._init_short_term_memory()

    assert stm is not None
    assert stm.backend == "database"
    assert stm.db_url == dsn


def test_short_term_memory_from_env_requires_dsn_for_unified_postgres(monkeypatch):
    from ksadk.memory.adk.short_term_memory import ShortTermMemory

    monkeypatch.delenv("KSADK_ADK_SESSION_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_ADK_SESSION_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_BACKEND", raising=False)
    monkeypatch.delenv("KSADK_STM_URL", raising=False)
    monkeypatch.delenv("KSADK_STM_DB_URL", raising=False)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.delenv("KSADK_SESSION_DSN", raising=False)

    with pytest.raises(ValueError, match="KSADK_SESSION_DSN"):
        ShortTermMemory.from_env()


def test_adk_runner_short_term_memory_uses_framework_specific_override(monkeypatch):
    monkeypatch.setenv("KSADK_STM_BACKEND", "sqlite")
    monkeypatch.setenv("KSADK_STM_PATH", "/tmp/shared-sessions.sqlite")
    monkeypatch.setenv("KSADK_ADK_SESSION_PATH", "/tmp/adk-private.sqlite")
    runner = _make_adk_runner()

    stm = runner._init_short_term_memory()

    assert stm is not None
    assert stm.local_database_path == "/tmp/adk-private.sqlite"


def test_platform_session_service_accepts_registered_backend(monkeypatch):
    def factory(config, project_dir):
        assert config.backend == "custom"
        assert project_dir == "/tmp/custom-project"
        return InMemorySessionService()

    register_session_backend("custom", factory)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "custom")

    service = create_session_service(project_dir="/tmp/custom-project")

    assert isinstance(service, InMemorySessionService)
