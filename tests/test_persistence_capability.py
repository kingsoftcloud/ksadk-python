from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_persistence_status_is_not_configured_without_postgres(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "local")
    monkeypatch.delenv("KSADK_SESSION_DSN", raising=False)

    status = (await get_persistence_status(use_cache=False))["Session"]

    assert status == {
        "Configured": False,
        "Status": "not_configured",
        "Ready": False,
        "Backend": "local",
        "SharedAcrossPods": False,
        "EffectiveFor": "new_runs_only",
        "Source": "none",
        "ReasonCode": "SESSION_STORE_NOT_CONFIGURED",
        "Reason": "Session storage is not configured",
    }


@pytest.mark.asyncio
async def test_persistence_status_reports_ready_without_exposing_dsn(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return True
            return 1

        async def close(self):
            return None

    async def connect(**kwargs):
        assert kwargs["dsn"] == "postgresql://user:secret@10.0.0.8:5432/appdb"
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv(
        "KSADK_SESSION_DSN",
        "postgresql://user:secret@10.0.0.8:5432/appdb",
    )

    status = (await get_persistence_status(connect=connect, use_cache=False))["Session"]

    assert status == {
        "Configured": True,
        "Status": "ready",
        "Ready": True,
        "Backend": "postgres",
        "SharedAcrossPods": True,
        "EffectiveFor": "new_runs_only",
        "Source": "explicit",
        "ReasonCode": "READY",
        "Reason": "",
    }
    assert "secret" not in repr(status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_dsn", "asyncpg_dsn"),
    [
        ("postgresql://user:secret@db.example.test/app", "postgresql://user:secret@db.example.test/app"),
        ("postgres://user:secret@db.example.test/app", "postgres://user:secret@db.example.test/app"),
        (
            "postgresql+asyncpg://user:secret@db.example.test/app",
            "postgresql://user:secret@db.example.test/app",
        ),
    ],
)
async def test_persistence_probe_accepts_postgres_driver_url_variants(
    configured_dsn, asyncpg_dsn
):
    """Catch passing an ADK SQLAlchemy URL directly to asyncpg."""
    from ksadk.sessions.persistence import probe_storage_target
    from ksadk.sessions.topology import StorageTarget

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return True
            return 1

        async def close(self):
            return None

    async def connect(**kwargs):
        assert kwargs["dsn"] == asyncpg_dsn
        return _Connection()

    status = await probe_storage_target(
        StorageTarget(backend="postgres", dsn=configured_dsn),
        connect=connect,
        use_cache=False,
    )

    assert status["Ready"] is True
    assert "secret" not in repr(status)


@pytest.mark.asyncio
async def test_persistence_status_uses_legacy_stm_postgres_fallbacks(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return True
            return 1

        async def close(self):
            return None

    async def connect(**kwargs):
        assert kwargs["dsn"] == "postgresql://user:secret@db.example.test/session"
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "")
    monkeypatch.setenv("KSADK_SESSION_DSN", "")
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setenv("KSADK_STM_BACKEND", "postgres")
    monkeypatch.setenv(
        "KSADK_STM_URL",
        "postgresql://user:secret@db.example.test/session",
    )

    status = (await get_persistence_status(connect=connect, use_cache=False))["Session"]

    assert status == {
        "Configured": True,
        "Status": "ready",
        "Ready": True,
        "Backend": "postgres",
        "SharedAcrossPods": True,
        "EffectiveFor": "new_runs_only",
        "Source": "explicit",
        "ReasonCode": "READY",
        "Reason": "",
    }
    assert "secret" not in repr(status)


@pytest.mark.asyncio
async def test_persistence_status_classifies_schema_permission_failure(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return False
            return 1

        async def close(self):
            return None

    async def connect(**_kwargs):
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://user:secret@10.0.0.8/appdb")

    status = (await get_persistence_status(connect=connect, use_cache=False))["Session"]

    assert status["Status"] == "error"
    assert status["Ready"] is False
    assert status["ReasonCode"] == "SCHEMA_PERMISSION_DENIED"
    assert "secret" not in status["Reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (type("InvalidPasswordError", (Exception,), {})(), "AUTH_FAILED"),
        (ModuleNotFoundError("asyncpg"), "DEPENDENCY_MISSING"),
    ],
)
async def test_persistence_status_classifies_stable_connection_errors(
    monkeypatch, error, reason_code
):
    from ksadk.sessions.persistence import get_persistence_status

    async def connect(**_kwargs):
        raise error

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://user:secret@10.0.0.8/appdb")

    status = (await get_persistence_status(connect=connect, use_cache=False))["Session"]

    assert status["Status"] == "error"
    assert status["Ready"] is False
    assert status["ReasonCode"] == reason_code
    assert "secret" not in repr(status)


@pytest.mark.asyncio
async def test_persistence_probe_does_not_swallow_task_cancellation(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    async def connect(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://user:secret@10.0.0.8/appdb")

    with pytest.raises(asyncio.CancelledError):
        await get_persistence_status(connect=connect, use_cache=False)


@pytest.mark.asyncio
async def test_status_probes_session_and_checkpoint_targets_independently(monkeypatch):
    """Catch probing the Session target for both dual-database stores."""
    from ksadk.sessions.persistence import get_persistence_status

    class _Connection:
        def __init__(self, ready: bool):
            self.ready = ready

        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return self.ready
            return 1

        async def close(self):
            return None

    async def connect(**kwargs):
        return _Connection(kwargs["dsn"].endswith("/session_db"))

    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://session.example.test/session_db")
    monkeypatch.setenv(
        "KSADK_CHECKPOINT_DSN", "postgresql://checkpoint.example.test/checkpoint_db"
    )

    status = await get_persistence_status(
        framework="langgraph", connect=connect, use_cache=False
    )

    assert status["Session"]["Source"] == "explicit"
    assert status["Checkpoint"]["Source"] == "explicit"
    assert status["Session"]["Ready"] is True
    assert status["Checkpoint"]["Ready"] is False
    assert status["Checkpoint"]["ReasonCode"] == "SCHEMA_PERMISSION_DENIED"
    assert "example.test" not in repr(status)


@pytest.mark.asyncio
async def test_status_probes_distinct_targets_concurrently(monkeypatch):
    from ksadk.sessions.persistence import get_persistence_status

    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    class _Connection:
        async def fetchval(self, query):
            return True if "has_schema_privilege" in query else 1

        async def close(self):
            return None

    async def connect(**kwargs):
        nonlocal started
        assert kwargs["dsn"].endswith(("/session_db", "/checkpoint_db"))
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://session.example.test/session_db")
    monkeypatch.setenv(
        "KSADK_CHECKPOINT_DSN", "postgresql://checkpoint.example.test/checkpoint_db"
    )
    task = asyncio.create_task(
        get_persistence_status(framework="langgraph", connect=connect, use_cache=False)
    )

    await asyncio.wait_for(both_started.wait(), timeout=0.2)
    release.set()
    status = await task

    assert status["Session"]["Ready"] is True
    assert status["Checkpoint"]["Ready"] is True


@pytest.mark.asyncio
async def test_checkpoint_unreachable_status_uses_checkpoint_reason_code(monkeypatch):
    """Catch returning the generic database error for a failed Checkpoint target."""
    from ksadk.sessions.persistence import get_persistence_status

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return True
            return 1

        async def close(self):
            return None

    async def connect(**kwargs):
        if kwargs["dsn"].endswith("/checkpoint_db"):
            raise OSError("unreachable")
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://session.example.test/session_db")
    monkeypatch.setenv(
        "KSADK_CHECKPOINT_DSN", "postgresql://checkpoint.example.test/checkpoint_db"
    )

    status = await get_persistence_status(
        framework="langgraph", connect=connect, use_cache=False
    )

    assert status["Session"]["Ready"] is True
    assert status["Checkpoint"]["ReasonCode"] == "CHECKPOINT_STORE_UNREACHABLE"


@pytest.mark.asyncio
async def test_status_deduplicates_same_target_fallback_probe(monkeypatch):
    """Catch opening two connections when Session is the Checkpoint fallback."""
    from ksadk.sessions.persistence import get_persistence_status

    calls = 0

    class _Connection:
        async def fetchval(self, query):
            if "has_schema_privilege" in query:
                return True
            return 1

        async def close(self):
            return None

    async def connect(**_kwargs):
        nonlocal calls
        calls += 1
        return _Connection()

    monkeypatch.setenv("KSADK_SESSION_DSN", "postgresql://session.example.test/session_db")
    monkeypatch.delenv("KSADK_CHECKPOINT_DSN", raising=False)

    status = await get_persistence_status(
        framework="langgraph", connect=connect, use_cache=False
    )

    assert calls == 1
    assert status["Session"]["Source"] == "explicit"
    assert status["Checkpoint"]["Source"] == "session_fallback"
    assert status["Session"] == {**status["Checkpoint"], "Source": "explicit"}

    from ksadk.sessions.persistence import gate_runtime_capabilities

    capabilities = gate_runtime_capabilities(
        {
            "Checkpoint": {
                "Supported": True,
                "Backend": "postgres",
                "Scope": "shared",
                "Durable": True,
                "SharedAcrossPods": True,
                "ResumeMode": "time_travel",
                "Reason": "",
            },
            "ResumeRun": {"Supported": True, "ResumeMode": "time_travel", "Reason": ""},
        },
        status["Session"],
        status["Checkpoint"],
    )

    assert capabilities["Checkpoint"]["Backend"] == "postgres"
    assert "NativeBackend" not in capabilities["Checkpoint"]
    assert "PersistenceGate" not in capabilities["Checkpoint"]


@pytest.mark.asyncio
async def test_single_target_probe_uses_a_generic_not_configured_reason():
    """Catch the reusable target probe emitting a doubled reason-code prefix."""
    from ksadk.sessions.persistence import probe_storage_target
    from ksadk.sessions.topology import StorageTarget

    status = await probe_storage_target(StorageTarget(backend="none"))

    assert status["ReasonCode"] == "STORE_NOT_CONFIGURED"


class _PostgresCheckpointRunner:
    detection_result = SimpleNamespace(
        type=SimpleNamespace(value="langgraph"),
        name="postgres-checkpoint-agent",
        description="",
    )

    def load_agent(self):
        return None

    def get_runtime_capabilities(self):
        return {
            "Framework": "langgraph",
            "Checkpoint": {
                "Supported": True,
                "Backend": "postgres",
                "Scope": "shared",
                "Durable": True,
                "SharedAcrossPods": True,
                "ResumeMode": "time_travel",
                "Reason": "",
            },
            "ResumeRun": {
                "Supported": True,
                "ResumeMode": "time_travel",
                "Reason": "",
            },
            "CancelRun": {"Supported": False},
        }


@pytest.mark.asyncio
async def test_bootstrap_exposes_independent_checkpoint_status_and_gates_resume(
    monkeypatch,
):
    """Catch bootstrap treating a ready Session store as a ready checkpoint store."""
    server_app_module = importlib.import_module("ksadk.server.app")
    server_app_module.set_runner(_PostgresCheckpointRunner())
    observed_frameworks = []

    async def persistence_status(*, framework=None):
        observed_frameworks.append(framework)
        return {
            "Session": {
                "Configured": True,
                "Status": "ready",
                "Ready": True,
                "Backend": "postgres",
                "SharedAcrossPods": True,
                "EffectiveFor": "new_runs_only",
                "Source": "explicit",
                "ReasonCode": "READY",
                "Reason": "",
            },
            "Checkpoint": {
                "Configured": True,
                "Status": "error",
                "Ready": False,
                "Backend": "postgres",
                "SharedAcrossPods": False,
                "EffectiveFor": "new_runs_only",
                "Source": "explicit",
                "ReasonCode": "CHECKPOINT_STORE_UNREACHABLE",
                "Reason": "Checkpoint storage is unreachable",
            },
        }

    monkeypatch.setattr(server_app_module, "get_persistence_status", persistence_status)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    capabilities = response.json()["Data"]["Capabilities"]
    assert observed_frameworks == ["langgraph"]
    assert capabilities["Persistence"]["Ready"] is True
    assert capabilities["CheckpointPersistence"]["Ready"] is False
    assert capabilities["RuntimeCapabilities"]["ResumeRun"]["ReasonCode"] == (
        "CHECKPOINT_STORE_UNREACHABLE"
    )
    assert capabilities["RuntimeCapabilities"]["Checkpoint"] == {
        "Supported": False,
        "Backend": "none",
        "NativeBackend": "postgres",
        "Scope": "shared",
        "Durable": False,
        "SharedAcrossPods": False,
        "ResumeMode": "none",
        "ReasonCode": "CHECKPOINT_STORE_UNREACHABLE",
        "Reason": "Checkpoint storage is unreachable",
        "PersistenceGate": {
            "BlockedStore": "checkpoint",
            "Source": "explicit",
            "ReasonCode": "CHECKPOINT_STORE_UNREACHABLE",
            "Reason": "Checkpoint storage is unreachable",
        },
    }


def test_persistence_gate_identifies_session_fallback_as_blocker():
    """Catch a gate report that blames Checkpoint when Session is unavailable."""
    from ksadk.sessions.persistence import gate_runtime_capabilities

    capabilities = gate_runtime_capabilities(
        {
            "Checkpoint": {
                "Supported": True,
                "Backend": "adk_invocation+postgres",
                "Scope": "invocation",
                "Durable": True,
                "SharedAcrossPods": True,
                "ResumeMode": "invocation_id",
                "Reason": "",
            },
            "ResumeRun": {
                "Supported": True,
                "ResumeMode": "invocation_id",
                "Reason": "",
            },
        },
        {
            "Ready": False,
            "Source": "checkpoint_fallback",
            "ReasonCode": "SESSION_STORE_UNREACHABLE",
            "Reason": "Session storage is unreachable",
        },
        {
            "Ready": True,
            "Source": "explicit",
            "ReasonCode": "READY",
            "Reason": "",
        },
    )

    assert capabilities["Checkpoint"]["Backend"] == "none"
    assert capabilities["Checkpoint"]["NativeBackend"] == "adk_invocation+postgres"
    assert capabilities["Checkpoint"]["PersistenceGate"] == {
        "BlockedStore": "session",
        "Source": "checkpoint_fallback",
        "ReasonCode": "SESSION_STORE_UNREACHABLE",
        "Reason": "Session storage is unreachable",
    }


@pytest.mark.asyncio
async def test_bootstrap_disables_checkpoint_when_postgres_is_not_configured(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    server_app_module.set_runner(_PostgresCheckpointRunner())

    async def not_configured(*, framework=None):
        return {
            "Configured": False,
            "Status": "not_configured",
            "Ready": False,
            "Backend": "local",
            "SharedAcrossPods": False,
            "EffectiveFor": "new_runs_only",
            "ReasonCode": "NOT_CONFIGURED",
            "Reason": "PostgreSQL persistence is not configured",
        }

    monkeypatch.setattr(server_app_module, "get_persistence_status", not_configured)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    capabilities = response.json()["Data"]["Capabilities"]
    assert capabilities["Persistence"]["Status"] == "not_configured"
    assert capabilities["CheckpointResumeCapability"]["Supported"] is False
    assert capabilities["RuntimeCapabilities"]["ResumeRun"]["ReasonCode"] == (
        "NOT_CONFIGURED"
    )
    assert capabilities["RunLifecycle"]["Checkpoints"] is False


@pytest.mark.asyncio
async def test_bootstrap_disables_checkpoint_when_configured_postgres_is_not_ready(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    server_app_module.set_runner(_PostgresCheckpointRunner())

    async def not_ready(*, framework=None):
        return {
            "Configured": True,
            "Status": "error",
            "Ready": False,
            "Backend": "postgres",
            "SharedAcrossPods": False,
            "EffectiveFor": "new_runs_only",
            "ReasonCode": "DB_UNREACHABLE",
            "Reason": "PostgreSQL persistence is unreachable",
        }

    monkeypatch.setattr(server_app_module, "get_persistence_status", not_ready)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    capabilities = response.json()["Data"]["Capabilities"]
    assert capabilities["Persistence"]["ReasonCode"] == "DB_UNREACHABLE"
    assert capabilities["CheckpointResumeCapability"]["Supported"] is False
    assert capabilities["RuntimeCapabilities"]["ResumeRun"] == {
        "Supported": False,
        "ResumeMode": "none",
        "ReasonCode": "DB_UNREACHABLE",
        "Reason": "PostgreSQL persistence is unreachable",
    }
    assert capabilities["RunLifecycle"]["Checkpoints"] is False


@pytest.mark.asyncio
async def test_bootstrap_awaits_runner_capability_preparation(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")

    class _PreparingRunner(_PostgresCheckpointRunner):
        prepared = False

        async def prepare_runtime_capabilities(self):
            self.prepared = True

        def get_runtime_capabilities(self):
            capabilities = super().get_runtime_capabilities()
            capabilities["Checkpoint"]["Supported"] = self.prepared
            capabilities["ResumeRun"]["Supported"] = self.prepared
            return capabilities

    active_runner = _PreparingRunner()
    server_app_module.set_runner(active_runner)

    async def ready(*, framework=None):
        return {
            "Configured": True,
            "Status": "ready",
            "Ready": True,
            "Backend": "postgres",
            "SharedAcrossPods": True,
            "EffectiveFor": "new_runs_only",
            "ReasonCode": "READY",
            "Reason": "",
        }

    monkeypatch.setattr(server_app_module, "get_persistence_status", ready)
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert active_runner.prepared is True
    assert response.json()["Data"]["Capabilities"]["ResumeRun"] is True


@pytest.mark.asyncio
async def test_bootstrap_capability_recovers_without_restarting_app(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    runner = _PostgresCheckpointRunner()
    server_app_module.set_runner(runner)
    ready = False

    async def changing_status(*, framework=None, use_cache=True):
        base = {
            "Configured": True,
            "Status": "ready" if ready else "error",
            "Ready": ready,
            "Backend": "postgres",
            "SharedAcrossPods": ready,
            "EffectiveFor": "new_runs_only",
            "Source": "explicit",
            "ReasonCode": "READY" if ready else "STORE_UNREACHABLE",
            "Reason": "" if ready else "PostgreSQL persistence is unreachable",
        }
        return {
            "Session": {
                **base,
                "ReasonCode": "READY" if ready else "SESSION_STORE_UNREACHABLE",
            },
            "Checkpoint": {
                **base,
                "ReasonCode": "READY" if ready else "CHECKPOINT_STORE_UNREACHABLE",
            },
        }

    monkeypatch.setattr(server_app_module, "get_persistence_status", changing_status)
    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        first = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "demo-agent"}
        )
        ready = True
        server_app_module.app.state.runtime.persistence_capability._snapshot = None
        second = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap", json={"AgentId": "demo-agent"}
        )

    assert first.json()["Data"]["Capabilities"]["ResumeRun"] is False
    assert second.json()["Data"]["Capabilities"]["ResumeRun"] is True
