from __future__ import annotations

import pytest

from ksadk.sessions.topology import resolve_persistence_topology


def _clear_persistence_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KSADK_SESSION_BACKEND",
        "AGENTENGINE_SESSION_BACKEND",
        "KSADK_STM_BACKEND",
        "KSADK_SESSION_DSN",
        "KSADK_STM_URL",
        "KSADK_STM_DB_URL",
        "KSADK_CHECKPOINT_DSN",
        "KSADK_ADK_SESSION_URL",
        "KSADK_LANGGRAPH_CHECKPOINT_DSN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("session_dsn", "checkpoint_dsn", "expected_session", "expected_checkpoint", "session_source", "checkpoint_source"),
    [
        ("", "", "", "", "none", "none"),
        (
            "postgresql://session.example.test/session_db",
            "",
            "postgresql://session.example.test/session_db",
            "postgresql://session.example.test/session_db",
            "explicit",
            "session_fallback",
        ),
        (
            "",
            "postgresql://checkpoint.example.test/checkpoint_db",
            "postgresql://checkpoint.example.test/checkpoint_db",
            "postgresql://checkpoint.example.test/checkpoint_db",
            "checkpoint_fallback",
            "explicit",
        ),
        (
            "postgresql://session.example.test/session_db",
            "postgresql://checkpoint.example.test/checkpoint_db",
            "postgresql://session.example.test/session_db",
            "postgresql://checkpoint.example.test/checkpoint_db",
            "explicit",
            "explicit",
        ),
    ],
)
def test_resolve_persistence_topology_routes_each_storage_target(
    monkeypatch: pytest.MonkeyPatch,
    session_dsn: str,
    checkpoint_dsn: str,
    expected_session: str,
    expected_checkpoint: str,
    session_source: str,
    checkpoint_source: str,
) -> None:
    """Catch a topology resolver that skips either documented fallback direction."""
    _clear_persistence_environment(monkeypatch)
    if session_dsn:
        monkeypatch.setenv("KSADK_SESSION_DSN", session_dsn)
    if checkpoint_dsn:
        monkeypatch.setenv("KSADK_CHECKPOINT_DSN", checkpoint_dsn)

    topology = resolve_persistence_topology(framework="langgraph")

    assert topology.session.dsn == expected_session
    assert topology.checkpoint.dsn == expected_checkpoint
    assert topology.session.source == session_source
    assert topology.checkpoint.source == checkpoint_source


def test_explicit_local_session_backend_does_not_fall_back_to_checkpoint_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch accidentally treating an explicit local Session choice as an unset default."""
    _clear_persistence_environment(monkeypatch)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.setenv(
        "KSADK_CHECKPOINT_DSN", "postgresql://checkpoint.example.test/checkpoint_db"
    )

    topology = resolve_persistence_topology(framework="langgraph")

    assert topology.session.backend == "local"
    assert topology.session.dsn == ""
    assert topology.session.explicit_local is True
    assert topology.checkpoint.dsn == "postgresql://checkpoint.example.test/checkpoint_db"


def test_explicit_custom_session_backend_keeps_its_session_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch routing a registered Session backend through PostgreSQL by mistake."""
    _clear_persistence_environment(monkeypatch)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "custom")
    monkeypatch.setenv("KSADK_SESSION_DSN", "custom://session.example.test/session")

    topology = resolve_persistence_topology()

    assert topology.session.backend == "custom"
    assert topology.session.dsn == "custom://session.example.test/session"
