from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ksadk.plugins.bridges.dsh import (
    DshBridgeHost,
    DshPluginInventory,
    DshProfileProjection,
    DshProfileRecoveryError,
)
from ksadk.plugins.providers.dsh_capabilities import (
    DshCapabilityTool,
    DshMcpConnectorLease,
    DshProfileCapabilityDescriptor,
    DshProfileCapabilityInventory,
    _inventory_digest,
)
from ksadk.studio import api_plugin_routes
from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService

_PLUGIN_ID = "@example/core-plugin"


@pytest.mark.parametrize("source", ["@xmanrui/dsh-im", "dsh-plugin", "@xmanrui/dsh-im@4.13.0"])
def test_install_request_accepts_names_and_exact_versions(source: str) -> None:
    assert api_plugin_routes.DshPluginInstallRequest(source=source).source == source


@pytest.mark.parametrize("source", [
    "--help", "@xmanrui/dsh-im@^4", "@xmanrui/dsh-im@beta",
    "https://example.com/plugin.tgz", "/tmp/plugin", "name\n",
])
def test_install_request_rejects_other_source_kinds(source: str) -> None:
    with pytest.raises(ValueError):
        api_plugin_routes.DshPluginInstallRequest(source=source)


def _descriptor() -> DshProfileCapabilityDescriptor:
    tool = DshCapabilityTool(
        name="fixture.echo",
        description="Echo one value",
        input_schema={"type": "object", "additionalProperties": False},
    )
    return DshProfileCapabilityDescriptor(
        dsh_version="0.1.2-rc.1",
        profile="web",
        profile_digest="sha256:" + "a" * 64,
        inventory_digest=_inventory_digest((tool,)),
        tools=(tool,),
    )


def _plugin(*, enabled: bool = True) -> DshPluginInventory:
    return DshPluginInventory(
        profile="web",
        name=_PLUGIN_ID,
        display_name="Core Plugin",
        version="1.0.0",
        requested_spec=f"{_PLUGIN_ID}@1.0.0",
        enabled=enabled,
    )


class _FakeBridge:
    host = DshBridgeHost(version="0.1.2-rc.1")

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeBridge":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def get_plugin(self, plugin_name: str) -> DshPluginInventory:
        assert plugin_name == _PLUGIN_ID
        return _plugin()

    def list_plugins(self) -> tuple[DshPluginInventory, ...]:
        return (_plugin(),)

    def project_profile(self) -> DshProfileProjection:
        return DshProfileProjection(
            profile="web",
            bundles=("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", _PLUGIN_ID),
            config_digest="sha256:" + "b" * 64,
            config_bytes=3,
            host_version="0.1.2-rc.1",
        )

    def set_enabled(self, plugin_name: str, *, enabled: bool) -> DshPluginInventory:
        assert plugin_name == _PLUGIN_ID
        return _plugin(enabled=enabled)


class _FakeCapabilities:
    def __init__(self) -> None:
        self.descriptor = _descriptor()
        self.refresh_count = 0
        self.lease = DshMcpConnectorLease(
            endpoint="http://127.0.0.1:43123/mcp",
            profile="web",
            profile_digest=self.descriptor.profile_digest,
            descriptor_digest=self.descriptor.descriptor_digest,
            _bearer_token="mcp-secret",
            _browser_token="browser-secret",
            web_route_count=2,
        )

    async def describe(self) -> DshProfileCapabilityDescriptor:
        return self.descriptor

    async def connector_lease(self) -> DshMcpConnectorLease:
        return self.lease

    async def inventory(self) -> DshProfileCapabilityInventory:
        return DshProfileCapabilityInventory(
            profile="web",
            profile_digest=self.descriptor.profile_digest,
            descriptor_digest=self.descriptor.descriptor_digest,
            inventory_digest=self.descriptor.inventory_digest,
            state="ready",
            pid=4000,
            tool_count=1,
            circuit_state="closed",
            consecutive_failures=0,
            retry_after_seconds=0,
        )

    async def capability_snapshot(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            descriptor=self.descriptor,
            tools=self.descriptor.tools,
            inventory=await self.inventory(),
            generation_id="dshgen_" + "g" * 32,
        )

    async def refresh(self) -> None:
        self.refresh_count += 1

    async def aclose(self) -> None:
        return None


def _headers(*, write: bool = False) -> dict[str, str]:
    headers = {"Origin": "http://testserver", "X-AgentKit-Session": "studio-session"}
    if write:
        headers["X-CSRF-Token"] = "csrf-token"
    return headers


@pytest.fixture
def studio_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    capabilities = _FakeCapabilities()
    service = StudioService(tmp_path, dsh_capability_service=capabilities)  # type: ignore[arg-type]
    monkeypatch.setattr(api_plugin_routes, "DshProfilePluginBridge", _FakeBridge)
    app = create_studio_app(
        tmp_path,
        service=service,
        session_token="studio-session",
        csrf_token="csrf-token",
    )
    return app, capabilities


@pytest.mark.asyncio
async def test_explicit_legacy_home_can_be_listed_but_new_core_cannot_mutate_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    marker = legacy / "old-session.jsonl"
    marker.write_text('{"version":2}\n')
    monkeypatch.setenv("KSADK_DSH_HOME", str(legacy))
    monkeypatch.setattr(api_plugin_routes, "DshProfilePluginBridge", _FakeBridge)
    service = StudioService(tmp_path, dsh_capability_service=_FakeCapabilities())
    app = create_studio_app(
        tmp_path, service=service, session_token="studio-session", csrf_token="csrf-token",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        listing = await client.get("/api/v1/plugin-ecosystems/dsh/plugins", headers=_headers())
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 1
        assert listing.json()["homeCompatibility"]["reason"] == "receipt_missing"
        response = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/plugins/{_PLUGIN_ID}:disable",
            headers=_headers(write=True),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DSH_HOME_VERSION_UNVERIFIED"
        assert "原版本工具链" in response.json()["error"]["message"]
    assert list(legacy.iterdir()) == [marker]
    assert marker.read_text() == '{"version":2}\n'


@pytest.mark.asyncio
async def test_layout_recovery_failure_keeps_studio_admission_suspended(studio_app, monkeypatch):
    app, _ = studio_app
    studio = app.state.studio_service
    events = []

    async def suspend():
        events.append("suspend")

    async def resume():
        events.append("resume")

    def migrate(_self, **_kwargs):
        raise DshProfileRecoveryError("fixture recovery failure")

    monkeypatch.setattr(studio.plugin_runs, "suspend_admission", suspend)
    monkeypatch.setattr(studio.plugin_runs, "resume_admission", resume)
    monkeypatch.setattr(_FakeBridge, "migrate_to_isolated_layout", migrate, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/plugin-ecosystems/dsh/profile:migrate-layout",
            headers=_headers(write=True), json={"acceptHostPermissions": True},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DSH_PROFILE_RECOVERY_REQUIRED"
    assert events == ["suspend"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_request", [False, True])
async def test_layout_migration_holds_reconfiguration_fence(
    studio_app, monkeypatch, cancel_request,
):
    app, capabilities = studio_app
    studio = app.state.studio_service
    started = threading.Event()
    release = threading.Event()
    events = []

    def migrate(_self, *, accept_host_permissions):
        assert accept_host_permissions
        assert capabilities.refresh_count == 1
        events.append("migration")
        started.set()
        assert release.wait(5)

    async def reconfigure(operation):
        events.append("suspend")
        await studio.reset_dsh_capability_state()
        try:
            return await operation()
        finally:
            events.append("resume")

    monkeypatch.setattr(_FakeBridge, "migrate_to_isolated_layout", migrate, raising=False)
    monkeypatch.setattr(studio, "reconfigure_dsh_profile", reconfigure)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        denied = await client.post(
            "/api/v1/plugin-ecosystems/dsh/profile:migrate-layout",
            headers=_headers(write=True), json={"acceptHostPermissions": False},
        )
        assert denied.status_code >= 400
        assert events == []
        request = asyncio.create_task(client.post(
            "/api/v1/plugin-ecosystems/dsh/profile:migrate-layout",
            headers=_headers(write=True), json={"acceptHostPermissions": True},
        ))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if cancel_request:
                for _ in range(2):
                    request.cancel()
                    await asyncio.sleep(0)
                assert events == ["suspend", "migration"]
                assert not request.done()
            release.set()
            if cancel_request:
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                response = await request
                assert response.status_code == 200
                assert response.json()["nodeLinker"] == "isolated"
            assert events == ["suspend", "migration", "resume"]
        finally:
            release.set()


@pytest.mark.asyncio
async def test_core_session_reuses_the_capability_host_generation(studio_app) -> None:  # type: ignore[no-untyped-def]
    app, capabilities = studio_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/plugin-ecosystems/dsh/core/session",
            headers=_headers(write=True),
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "protocolVersion": "ksadk.dsh-core-runtime/v1",
        "version": "0.1.2-rc.1",
        "profile": "web",
        "endpoint": "http://127.0.0.1:43123/",
        "browserUrl": "http://127.0.0.1:43123/?token=browser-secret",
    }
    assert "mcp-secret" not in response.text
    assert capabilities.refresh_count == 0


@pytest.mark.asyncio
async def test_capability_metadata_excludes_ephemeral_credentials(studio_app) -> None:  # type: ignore[no-untyped-def]
    app, _capabilities = studio_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        capabilities = await client.get(
            "/api/v1/plugin-ecosystems/dsh/capabilities",
            headers=_headers(),
        )
        profile = await client.get(
            "/api/v1/plugin-ecosystems/dsh/profile",
            headers=_headers(),
        )

    assert capabilities.status_code == 200
    encoded = json.dumps(capabilities.json())
    assert "endpoint" not in encoded.lower()
    assert "mcp-secret" not in encoded
    assert "browser-secret" not in encoded
    assert profile.status_code == 200
    assert "clientBundles" not in profile.json()


@pytest.mark.asyncio
async def test_profile_mutation_restarts_the_single_core_generation(
    studio_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    app, capabilities = studio_app
    studio = app.state.studio_service

    async def local_reconfigure(operation):  # type: ignore[no-untyped-def]
        await studio.reset_dsh_capability_state()
        return await operation()

    monkeypatch.setattr(studio, "reconfigure_dsh_profile", local_reconfigure)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/plugins/{_PLUGIN_ID}:disable",
            headers=_headers(write=True),
        )

    assert response.status_code == 200, response.text
    assert capabilities.refresh_count == 1
