"""Transactional PluginHost tests: profile switch is stage-before-dispose."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.resolver import PluginRegistry


def _manifest(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    definition: str,
    slot: str,
    requires: list[dict] | None = None,
    permissions: list[str] | None = None,
    digit: str = "1",
) -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": plugin_id, "version": version},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.plugins:factory",
                "provides": [
                    {"definition": definition, "slot": slot, "mode": "unique"}
                ],
                "requires": requires or [],
                "permissions": permissions or [],
                "isolation": "process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {"source": "builtin", "digest": "sha256:" + digit * 64},
            },
        }
    )


def _profile(version: str = "1.0.0") -> CompositionProfile:
    return CompositionProfile.model_validate(
        {"agentProvider": {"ref": f"plugin://io.ksadk.provider@{version}"}}
    )


@dataclass
class _Runtime:
    plugin_id: str
    calls: list[str]
    healthy: bool = True

    async def start(self) -> None:
        self.calls.append(f"start:{self.plugin_id}")

    async def health(self) -> bool:
        self.calls.append(f"health:{self.plugin_id}")
        return self.healthy

    async def drain(self) -> None:
        self.calls.append(f"drain:{self.plugin_id}")

    async def dispose(self) -> None:
        self.calls.append(f"dispose:{self.plugin_id}")


class _Factory:
    def __init__(self, calls: list[str], *, unhealthy_versions: set[str] | None = None) -> None:
        self.calls = calls
        self.unhealthy_versions = unhealthy_versions or set()

    async def stage(self, manifest, *, profile, services):  # noqa: ANN001, ARG002
        self.calls.append(f"stage:{manifest.metadata.id}@{manifest.metadata.version}")
        return _Runtime(
            plugin_id=f"{manifest.metadata.id}@{manifest.metadata.version}",
            calls=self.calls,
            healthy=manifest.metadata.version not in self.unhealthy_versions,
        )


def _host(*, provider_versions: tuple[str, ...] = ("1.0.0",), permissions=None):
    event_store = _manifest(
        "io.ksadk.event-store",
        definition="session.event-store/v1",
        slot="session.events",
        digit="2",
    )
    providers = [
        _manifest(
            "io.ksadk.provider",
            version=version,
            definition="agent.provider/v1",
            slot="agent.execution",
            requires=[{"definition": "session.event-store/v1", "version": ">=1,<2"}],
            permissions=permissions,
            digit=str(index + 3),
        )
        for index, version in enumerate(provider_versions)
    ]
    calls: list[str] = []
    provider_factory = _Factory(
        calls,
        unhealthy_versions={"2.0.0"} if "2.0.0" in provider_versions else set(),
    )
    host = PluginHost(
        PluginRegistry([event_store, *providers]),
        {
            "io.ksadk.event-store": _Factory(calls),
            "io.ksadk.provider": provider_factory,
        },
    )
    return host, calls


@pytest.mark.asyncio
async def test_host_stages_dependencies_then_disposes_reverse_order() -> None:
    host, calls = _host()

    inventory = await host.apply(_profile())
    assert [item.id for item in inventory.plugins] == [
        "io.ksadk.event-store",
        "io.ksadk.provider",
    ]
    assert calls[:6] == [
        "stage:io.ksadk.event-store@1.0.0",
        "stage:io.ksadk.provider@1.0.0",
        "start:io.ksadk.event-store@1.0.0",
        "health:io.ksadk.event-store@1.0.0",
        "start:io.ksadk.provider@1.0.0",
        "health:io.ksadk.provider@1.0.0",
    ]

    await host.dispose()
    assert calls[-4:] == [
        "drain:io.ksadk.provider@1.0.0",
        "dispose:io.ksadk.provider@1.0.0",
        "drain:io.ksadk.event-store@1.0.0",
        "dispose:io.ksadk.event-store@1.0.0",
    ]
    assert host.inventory() is None


@pytest.mark.asyncio
async def test_failed_candidate_keeps_old_graph_active_and_cleans_candidate() -> None:
    host, calls = _host(provider_versions=("1.0.0", "2.0.0"))
    stable = await host.apply(_profile("1.0.0"))

    with pytest.raises(PluginHostError, match="failed health") as captured:
        await host.apply(_profile("2.0.0"))
    assert captured.value.code == "plugin_health_failed"
    assert host.inventory() == stable
    assert "dispose:io.ksadk.provider@2.0.0" in calls
    assert "dispose:io.ksadk.provider@1.0.0" not in calls


@pytest.mark.asyncio
async def test_cancelled_committed_switch_finishes_old_graph_cleanup() -> None:
    host, calls = _host(provider_versions=("1.0.0", "3.0.0"))
    await host.apply(_profile("1.0.0"))
    old_provider = host._active.plugins[-1].runtime
    original_drain = old_provider.drain
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()

    async def blocked_drain() -> None:
        drain_started.set()
        await allow_drain.wait()
        await original_drain()

    old_provider.drain = blocked_drain  # type: ignore[method-assign]
    switch = asyncio.create_task(host.apply(_profile("3.0.0")))
    await drain_started.wait()

    switch.cancel()
    await asyncio.sleep(0)
    assert switch.done() is False
    allow_drain.set()
    with pytest.raises(asyncio.CancelledError):
        await switch

    assert host.inventory() is not None
    assert host.inventory().plugins[-1].version == "3.0.0"
    assert "dispose:io.ksadk.provider@1.0.0" in calls
    assert "dispose:io.ksadk.event-store@1.0.0" in calls

    calls_after_cleanup = list(calls)
    recovered = await host.apply(_profile("3.0.0"))
    assert recovered.plugins[-1].version == "3.0.0"
    assert calls == calls_after_cleanup


@pytest.mark.asyncio
async def test_repeated_cancel_cleans_candidate_and_retry_stages_fresh_graph() -> None:
    host, calls = _host(provider_versions=("1.0.0", "3.0.0"))
    stable = await host.apply(_profile("1.0.0"))
    provider_factory = host._factories["io.ksadk.provider"]
    original_stage = provider_factory.stage
    health_started = asyncio.Event()
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()

    async def stage_candidate(manifest, *, profile, services):  # noqa: ANN001
        runtime = await original_stage(manifest, profile=profile, services=services)
        if manifest.metadata.version != "3.0.0":
            return runtime
        original_drain = runtime.drain

        async def blocked_health() -> bool:
            health_started.set()
            await asyncio.Event().wait()
            return True

        async def blocked_drain() -> None:
            drain_started.set()
            await allow_drain.wait()
            await original_drain()

        runtime.health = blocked_health  # type: ignore[method-assign]
        runtime.drain = blocked_drain  # type: ignore[method-assign]
        return runtime

    provider_factory.stage = stage_candidate  # type: ignore[method-assign]
    switch = asyncio.create_task(host.apply(_profile("3.0.0")))
    await health_started.wait()
    switch.cancel()
    await drain_started.wait()

    switch.cancel()
    await asyncio.sleep(0)
    assert switch.done() is False
    allow_drain.set()
    with pytest.raises(asyncio.CancelledError):
        await switch

    assert host.inventory() == stable
    assert "dispose:io.ksadk.provider@3.0.0" in calls
    assert "dispose:io.ksadk.provider@1.0.0" not in calls

    provider_factory.stage = original_stage  # type: ignore[method-assign]
    recovered = await host.apply(_profile("3.0.0"))
    assert recovered.plugins[-1].version == "3.0.0"
    assert calls.count("stage:io.ksadk.provider@3.0.0") == 2


@pytest.mark.asyncio
async def test_permission_rejection_happens_before_stage() -> None:
    host, calls = _host(permissions=["network:model-endpoint"])

    with pytest.raises(PluginHostError, match="unapproved permissions") as captured:
        await host.apply(_profile())
    assert captured.value.code == "plugin_permission_denied"
    assert calls == []


def test_preflight_rejects_missing_factory_without_running_a_candidate() -> None:
    host, calls = _host()
    host._factories.pop("io.ksadk.provider")  # test the explicit host boundary

    with pytest.raises(PluginHostError, match="no factory") as captured:
        host.preflight(_profile())
    assert captured.value.code == "plugin_factory_unavailable"
    assert calls == []
