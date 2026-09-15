from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _status(ready: bool) -> dict[str, dict[str, object]]:
    state = {
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
        "Session": {**state, "ReasonCode": "READY" if ready else "SESSION_STORE_UNREACHABLE"},
        "Checkpoint": {
            **state,
            "ReasonCode": "READY" if ready else "CHECKPOINT_STORE_UNREACHABLE",
        },
    }


class _Runner:
    detection_result = SimpleNamespace(type=SimpleNamespace(value="langgraph"))

    def __init__(self) -> None:
        self.refreshes = 0

    async def refresh_runtime_capabilities(self) -> None:
        self.refreshes += 1

    def get_runtime_capabilities(self):
        return {
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
        }


class _DegradedRunner(_Runner):
    def get_runtime_capabilities(self):
        capabilities = super().get_runtime_capabilities()
        capabilities["Checkpoint"].update(
            {
                "Supported": False,
                "Durable": False,
                "ReasonCode": "CHECKPOINT_STORE_UNREACHABLE",
                "Reason": "Managed checkpoint initialization failed",
            }
        )
        capabilities["ResumeRun"].update(
            {
                "Supported": False,
                "ReasonCode": "CHECKPOINT_STORE_UNREACHABLE",
                "Reason": "Managed checkpoint initialization failed",
            }
        )
        return capabilities


class _FailingRefreshRunner(_Runner):
    async def refresh_runtime_capabilities(self) -> None:
        raise ConnectionError("sensitive runner database endpoint")


class _SessionService:
    def __init__(self, *, degraded: bool) -> None:
        self.degraded = degraded


class _RecoveringSessionService(_SessionService):
    def __init__(self) -> None:
        super().__init__(degraded=True)
        self.refreshes = 0

    async def refresh_persistence_capability(self) -> bool:
        self.refreshes += 1
        self.degraded = False
        return True


@pytest.mark.asyncio
async def test_coordinator_transitions_degraded_ready_degraded_without_restart():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    outcomes = iter([False, True, False])

    async def provider(*, framework=None, use_cache=True):
        assert framework == "langgraph"
        assert use_cache is False
        return _status(next(outcomes))

    coordinator = PersistenceCapabilityCoordinator(background=False)
    runner = _Runner()

    first = await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )
    second = await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )
    third = await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )

    assert [first.state, second.state, third.state] == ["degraded", "ready", "degraded"]
    assert first.runtime_capabilities["ResumeRun"]["Supported"] is False
    assert second.runtime_capabilities["ResumeRun"]["Supported"] is True
    assert third.runtime_capabilities["ResumeRun"]["Supported"] is False
    assert [first.generation, second.generation, third.generation] == [1, 2, 3]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_projects_runner_degradation_as_no_effective_backend():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    async def provider(*, framework=None, use_cache=True):
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    snapshot = await coordinator.get_snapshot(
        runner=_DegradedRunner(),
        framework="langgraph",
        status_provider=provider,
        force=True,
    )

    checkpoint = snapshot.runtime_capabilities["Checkpoint"]
    assert snapshot.state == "degraded"
    assert checkpoint["Backend"] == "none"
    assert checkpoint["NativeBackend"] == "postgres"
    assert checkpoint["PersistenceGate"]["BlockedStore"] == "checkpoint"
    assert checkpoint["PersistenceGate"]["Source"] == "runtime"
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_converts_refresh_exception_to_safe_degraded_snapshot():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    async def provider(*, framework=None, use_cache=True):
        raise ConnectionError("sensitive database endpoint")

    coordinator = PersistenceCapabilityCoordinator(background=False)
    snapshot = await coordinator.get_snapshot(
        runner=_Runner(), framework="langgraph", status_provider=provider, force=True
    )

    assert snapshot.state == "degraded"
    assert snapshot.resume_supported is False
    assert snapshot.session_persistence["ReasonCode"] == "PERSISTENCE_REFRESH_FAILED"
    assert "sensitive" not in snapshot.session_persistence["Reason"]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_converts_runner_refresh_exception_to_safe_runtime_gate():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    async def provider(*, framework=None, use_cache=True):
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    snapshot = await coordinator.get_snapshot(
        runner=_FailingRefreshRunner(),
        framework="langgraph",
        status_provider=provider,
        force=True,
    )

    checkpoint = snapshot.runtime_capabilities["Checkpoint"]
    assert snapshot.state == "degraded"
    assert checkpoint["ReasonCode"] == "RUNTIME_CAPABILITY_REFRESH_FAILED"
    assert checkpoint["PersistenceGate"]["Source"] == "runtime"
    assert "sensitive" not in checkpoint["Reason"]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_gates_ready_probe_while_session_service_is_degraded():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    service = _SessionService(degraded=True)

    async def provider(*, framework=None, use_cache=True):
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    runner = _Runner()
    first = await coordinator.get_snapshot(
        runner=runner,
        framework="langgraph",
        status_provider=provider,
        session_service_provider=lambda: service,
        force=True,
    )
    service.degraded = False
    second = await coordinator.get_snapshot(
        runner=runner,
        framework="langgraph",
        status_provider=provider,
        session_service_provider=lambda: service,
        force=True,
    )

    assert first.state == "degraded"
    assert first.session_persistence["ReasonCode"] == "SESSION_STORE_DEGRADED"
    assert first.runtime_capabilities["ResumeRun"]["Supported"] is False
    assert second.state == "ready"
    assert second.runtime_capabilities["ResumeRun"]["Supported"] is True
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_refreshes_degraded_session_service_before_gating():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    service = _RecoveringSessionService()

    async def provider(*, framework=None, use_cache=True):
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    snapshot = await coordinator.get_snapshot(
        runner=_Runner(),
        framework="langgraph",
        status_provider=provider,
        session_service_provider=lambda: service,
        force=True,
    )

    assert service.refreshes == 1
    assert snapshot.state == "ready"
    assert snapshot.resume_supported is True
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_invalidate_cancels_old_background_loop():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    async def provider(*, framework=None, use_cache=True):
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(refresh_interval=30.0)
    runner = _Runner()
    await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )
    old_background = coordinator._background_task
    assert old_background is not None

    coordinator.invalidate(_Runner())
    await asyncio.sleep(0)

    assert old_background.cancelled()
    assert coordinator._background_task is None
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_coalesces_concurrent_refreshes():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(*, framework=None, use_cache=True):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    runner = _Runner()
    requests = [
        asyncio.create_task(
            coordinator.get_snapshot(
                runner=runner, framework="langgraph", status_provider=provider, force=True
            )
        )
        for _ in range(3)
    ]
    await entered.wait()
    release.set()
    snapshots = await asyncio.gather(*requests)

    assert calls == 1
    assert runner.refreshes == 1
    assert {snapshot.generation for snapshot in snapshots} == {1}
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_failure_backoff_caps_at_refresh_interval():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    now = 100.0

    def clock():
        return now

    async def provider(*, framework=None, use_cache=True):
        return _status(False)

    coordinator = PersistenceCapabilityCoordinator(
        clock=clock, refresh_interval=30.0, initial_retry_delay=5.0, background=False
    )
    runner = _Runner()
    delays = []
    for _ in range(5):
        snapshot = await coordinator.get_snapshot(
            runner=runner, framework="langgraph", status_provider=provider, force=True
        )
        delays.append(snapshot.next_refresh_at - now)

    assert delays == [5.0, 10.0, 20.0, 30.0, 30.0]
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_timeout_returns_last_fail_closed_snapshot():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    release = asyncio.Event()
    calls = 0

    async def provider(*, framework=None, use_cache=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _status(False)
        await release.wait()
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    runner = _Runner()
    first = await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )
    timed_out = await coordinator.get_snapshot(
        runner=runner,
        framework="langgraph",
        status_provider=provider,
        force=True,
        wait_timeout=0.01,
    )

    assert timed_out is first
    assert timed_out.runtime_capabilities["ResumeRun"]["Supported"] is False
    release.set()
    await asyncio.sleep(0)
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_timeout_never_returns_stale_ready_snapshot():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    release = asyncio.Event()
    calls = 0

    async def provider(*, framework=None, use_cache=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _status(True)
        await release.wait()
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator(background=False)
    runner = _Runner()
    ready = await coordinator.get_snapshot(
        runner=runner, framework="langgraph", status_provider=provider, force=True
    )
    timed_out = await coordinator.get_snapshot(
        runner=runner,
        framework="langgraph",
        status_provider=provider,
        force=True,
        wait_timeout=0.01,
    )

    assert ready.resume_supported is True
    assert timed_out.resume_supported is False
    assert timed_out.state == "checking"
    assert timed_out.runtime_capabilities["Checkpoint"]["Backend"] == "none"
    release.set()
    await asyncio.sleep(0)
    await coordinator.aclose()


@pytest.mark.asyncio
async def test_coordinator_start_eagerly_refreshes_and_runs_background_loop():
    from ksadk.server.persistence_capability import PersistenceCapabilityCoordinator

    entered = asyncio.Event()

    async def provider(*, framework=None, use_cache=True):
        entered.set()
        return _status(True)

    coordinator = PersistenceCapabilityCoordinator()
    coordinator.start(
        runner=_Runner(),
        framework="langgraph",
        status_provider=provider,
    )
    await asyncio.wait_for(entered.wait(), timeout=0.1)
    await coordinator._refresh_task

    assert coordinator.snapshot is not None
    assert coordinator.snapshot.state == "ready"
    assert coordinator._background_task is not None
    await coordinator.aclose()
