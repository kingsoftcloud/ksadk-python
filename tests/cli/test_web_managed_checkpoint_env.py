"""Web startup leaves checkpoint choice to the unified runner policy."""

import os

import pytest

from ksadk.cli import cmd_web
from ksadk.sessions.topology import resolve_persistence_topology


def _clear_persistence_env(monkeypatch):
    for name in (
        *cmd_web._CHECKPOINT_ENV_NAMES,
        *cmd_web._SESSION_ENV_NAMES,
        *cmd_web._STM_ENV_NAMES,
        *cmd_web._LOCAL_UI_ENV_NAMES,
        "KSADK_LANGGRAPH_AUTO_CHECKPOINT",
        "AGENT_KERNEL_ENABLED",
        "AGENT_KERNEL_STORE_DRIVER",
    ):
        # Startup writes os.environ directly. Register every original value,
        # including absence, so its newly created keys are undone after tests.
        monkeypatch.setenv(name, os.environ.get(name, ""))
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clean_persistence_env(monkeypatch):
    _clear_persistence_env(monkeypatch)


def test_web_persistence_fixture_restores_new_environment_values(tmp_path, monkeypatch):
    names = (
        *cmd_web._CHECKPOINT_ENV_NAMES,
        *cmd_web._SESSION_ENV_NAMES,
        *cmd_web._STM_ENV_NAMES,
        *cmd_web._LOCAL_UI_ENV_NAMES,
        "KSADK_LANGGRAPH_AUTO_CHECKPOINT",
        "AGENT_KERNEL_ENABLED",
        "AGENT_KERNEL_STORE_DRIVER",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    with pytest.MonkeyPatch.context() as runtime_env:
        _clear_persistence_env(runtime_env)
        cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")
        assert os.environ["AGENTENGINE_UI_DIR"] == str(tmp_path / ".agentengine/ui")
        assert os.environ["KSADK_SESSION_BACKEND"] == "local"
        assert os.environ["KSADK_SESSION_PATH"] == str(tmp_path / ".agentengine/ui/sessions.sqlite")

    remaining = sorted(set(names) & set(os.environ))
    assert not remaining, f"Runtime persistence keys leaked after teardown: {remaining}"


@pytest.mark.parametrize("key", ["KSADK_CHECKPOINT_DSN", "KSADK_LANGGRAPH_CHECKPOINT_DSN"])
@pytest.mark.parametrize("source", ["shell", "dotenv"])
def test_web_preserves_checkpoint_pg_configuration(tmp_path, monkeypatch, key, source):
    dsn = "postgresql://test:test@checkpoint.example:5432/test"
    monkeypatch.setenv(key, dsn)
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "1")
    if source == "dotenv":
        (tmp_path / ".env").write_text(f"{key}={dsn}\n")

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    target = resolve_persistence_topology(framework="langgraph").checkpoint
    assert target.backend == "postgres"
    assert target.dsn == dsn
    assert os.environ[key] == dsn
    assert "KSADK_CHECKPOINT_BACKEND" not in os.environ
    assert "KSADK_CHECKPOINT_PATH" not in os.environ


@pytest.mark.parametrize("auto", [None, "0", "1"])
def test_web_does_not_choose_checkpoint_or_enable_auto_mode(tmp_path, monkeypatch, auto):
    if auto is not None:
        monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", auto)

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    assert os.getenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT") == auto
    assert "KSADK_CHECKPOINT_BACKEND" not in os.environ
    assert "KSADK_CHECKPOINT_PATH" not in os.environ
    assert resolve_persistence_topology(framework="langgraph").checkpoint.backend == "none"
    assert os.environ["KSADK_SESSION_BACKEND"] == "local"
    assert os.environ["KSADK_SESSION_PATH"] == str(tmp_path / ".agentengine/ui/sessions.sqlite")


@pytest.mark.parametrize("key", ["KSADK_SESSION_DSN", "KSADK_STM_URL", "KSADK_STM_DB_URL"])
def test_web_keeps_dotenv_session_pg_available_to_auto_checkpoint(tmp_path, monkeypatch, key):
    dsn = "postgresql://test:test@session.example:5432/test"
    monkeypatch.setenv(key, dsn)
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "1")
    (tmp_path / ".env").write_text(f"{key}={dsn}\n")

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    topology = resolve_persistence_topology(framework="langgraph")
    assert topology.session.backend == "postgres"
    assert topology.session.dsn == dsn
    assert topology.checkpoint.backend == "postgres"
    assert topology.checkpoint.dsn == dsn
    assert topology.checkpoint.source == "session_fallback"
    assert os.environ[key] == dsn


@pytest.mark.parametrize(
    "key", ["KSADK_SESSION_BACKEND", "AGENTENGINE_SESSION_BACKEND", "KSADK_STM_BACKEND"]
)
def test_web_does_not_hide_dotenv_pg_selection_without_dsn(tmp_path, monkeypatch, key):
    monkeypatch.setenv(key, "postgres")
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "1")
    (tmp_path / ".env").write_text(f"{key}=postgres\n")

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    topology = resolve_persistence_topology(framework="langgraph")
    assert os.environ[key] == "postgres"
    assert topology.session.backend == "postgres"
    assert topology.session.dsn == ""
    assert topology.checkpoint.dsn == ""
    assert "KSADK_CHECKPOINT_BACKEND" not in os.environ
    assert not (tmp_path / ".agentengine/ui/checkpoints.sqlite").exists()


@pytest.mark.parametrize("backend", ["local", "sqlite", "memory", "postgres"])
def test_web_leaves_explicit_checkpoint_values_unchanged(tmp_path, monkeypatch, backend):
    values = {
        "KSADK_CHECKPOINT_BACKEND": backend,
        "KSADK_CHECKPOINT_PATH": str(tmp_path / "custom.sqlite"),
        "KSADK_LANGGRAPH_CHECKPOINT_DSN": "postgresql://test:test@checkpoint.example:5432/test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    assert {key: os.environ[key] for key in values} == values


@pytest.mark.parametrize("framework,auto", [("langgraph", "0"), ("adk", "1")])
def test_original_session_defaults_remain_for_other_modes(tmp_path, monkeypatch, framework, auto):
    dsn = "postgresql://test:test@session.example:5432/test"
    monkeypatch.setenv("KSADK_SESSION_DSN", dsn)
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", auto)
    (tmp_path / ".env").write_text(f"KSADK_SESSION_DSN={dsn}\n")

    cmd_web.configure_local_runtime_persistence(tmp_path, framework)

    assert os.environ["KSADK_SESSION_BACKEND"] == "local"
    assert "KSADK_SESSION_DSN" not in os.environ
    assert os.environ["KSADK_SESSION_PATH"] == str(tmp_path / ".agentengine/ui/sessions.sqlite")


@pytest.mark.parametrize("framework", ["langgraph", "adk"])
def test_postgres_kernel_keeps_platform_session_fallback(tmp_path, monkeypatch, framework):
    from ksadk.kernel.session_storage import select_kernel_session_storage

    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "postgres")
    monkeypatch.setenv("KSADK_LANGGRAPH_AUTO_CHECKPOINT", "1")
    monkeypatch.setenv(
        "KSADK_LANGGRAPH_CHECKPOINT_DSN", "postgresql://fixture@checkpoint.example/native"
    )

    cmd_web.configure_local_runtime_persistence(tmp_path, framework)

    assert not set((*cmd_web._SESSION_ENV_NAMES, *cmd_web._STM_ENV_NAMES)) & set(os.environ)
    selection = select_kernel_session_storage("postgresql://fixture@platform.example/kernel")
    assert selection.target.source == "platform_kernel_fallback"
    assert selection.target.backend == "postgres"


@pytest.mark.parametrize("backend", ["local", "sqlite", "memory"])
def test_postgres_kernel_does_not_hide_explicit_local_conflict(tmp_path, monkeypatch, backend):
    from ksadk.kernel.session_storage import select_kernel_session_storage

    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "postgres")
    monkeypatch.setenv("KSADK_SESSION_BACKEND", backend)
    (tmp_path / ".env").write_text(f"KSADK_SESSION_BACKEND={backend}\n")

    cmd_web.configure_local_runtime_persistence(tmp_path, "langgraph")

    assert os.environ["KSADK_SESSION_BACKEND"] == backend
    with pytest.raises(RuntimeError, match="KERNEL_SESSION_REQUIRES_POSTGRES"):
        select_kernel_session_storage("postgresql://fixture@platform.example/kernel")
