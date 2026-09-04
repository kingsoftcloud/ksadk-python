from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ksadk.plugins.bridges.dsh import (
    DshBridgeHost,
    DshClientBundle,
    DshPluginInventory,
    DshProfileProjection,
)
from ksadk.plugins.providers.dsh_capabilities import (
    DshCapabilityTool,
    DshProfileCapabilityDescriptor,
    DshProfileCapabilityInventory,
    _inventory_digest,
)
from ksadk.studio import api_plugin_routes
from ksadk.studio.api import create_studio_app
from ksadk.studio.dsh_capability_service import dsh_ui_mcp_call_id
from ksadk.studio.dsh_ui_sandbox import (
    DSH_UI_FRAME_ORIGIN,
    DshUiSandboxError,
    DshUiSandboxSessionStore,
)
from ksadk.studio.service import StudioService

_PLUGIN_ID = "@example/sandbox-ui"
_BUNDLE = b"window.__fixtureSandboxBundleLoaded = true;\n"
_BUNDLE_DIGEST = f"sha256:{hashlib.sha256(_BUNDLE).hexdigest()}"
_GENERATION_ID = "dshgen_" + "g" * 32


def _descriptor() -> DshProfileCapabilityDescriptor:
    tool = DshCapabilityTool(
        name="fixture.echo",
        description="Echo one value",
        input_schema={"type": "object", "additionalProperties": False},
    )
    return DshProfileCapabilityDescriptor(
        dsh_version="0.1.1-rc.2",
        profile="studio",
        profile_digest="sha256:" + "a" * 64,
        inventory_digest=_inventory_digest((tool,)),
        tools=(tool,),
    )


def _plugin() -> DshPluginInventory:
    return DshPluginInventory(
        profile="studio",
        name=_PLUGIN_ID,
        display_name="Sandbox Fixture",
        version="1.0.0",
        requested_spec=f"{_PLUGIN_ID}@1.0.0",
        enabled=True,
        client_bundle=DshClientBundle(
            digest=_BUNDLE_DIGEST,
            content_bytes=len(_BUNDLE),
            external=(),
            inject=(),
            compatible=True,
        ),
    )


class _FakeBridge:
    host = DshBridgeHost(version="0.1.1-rc.2")

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
            profile="studio",
            bundles=(_PLUGIN_ID,),
            config_digest="sha256:" + "b" * 64,
            config_bytes=3,
            host_version="0.1.1-rc.2",
        )

    def read_client_bundle(self, plugin_name: str, *, expected_digest: str) -> bytes:
        assert plugin_name == _PLUGIN_ID
        assert expected_digest == _BUNDLE_DIGEST
        return _BUNDLE

    def set_enabled(self, plugin_name: str, *, enabled: bool) -> DshPluginInventory:
        assert plugin_name == _PLUGIN_ID
        return _plugin().model_copy(update={"enabled": enabled})


class _FakeCapabilities:
    def __init__(self) -> None:
        self.descriptor = _descriptor()
        self.generation_id = _GENERATION_ID
        self.cancelled: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any], int]] = []
        self.call_started = asyncio.Event()
        self.release_call = asyncio.Event()
        self.refresh_count = 0

    async def describe(self) -> DshProfileCapabilityDescriptor:
        return self.descriptor

    async def descriptor_generation(self):  # type: ignore[no-untyped-def]
        return self.descriptor, self.generation_id

    async def list_tools(self, **_kwargs: Any) -> tuple[DshCapabilityTool, ...]:
        return self.descriptor.tools

    async def inventory(self) -> DshProfileCapabilityInventory:
        return DshProfileCapabilityInventory(
            profile=self.descriptor.profile,
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
            generation_id=self.generation_id,
        )

    async def call_tool(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        deadline_ms: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((call_id, tool_name, dict(arguments), deadline_ms))
        if arguments.get("wait"):
            self.call_started.set()
            await self.release_call.wait()
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    async def cancel(self, call_id: str) -> bool:
        self.cancelled.append(call_id)
        return True

    async def refresh(self) -> None:
        self.refresh_count += 1

    async def aclose(self) -> None:
        return None


def _request_headers(*, write: bool = False) -> dict[str, str]:
    headers = {
        "Origin": "http://testserver",
        "X-AgentKit-Session": "studio-session",
    }
    if write:
        headers["X-CSRF-Token"] = "csrf-token"
    return headers


def _request_message(
    session: dict[str, Any],
    *,
    request_id: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    handshake = session["handshake"]
    return {
        "protocolVersion": "agentkit.dsh-ui/v1",
        "kind": "request",
        "sessionId": session["uiSessionId"],
        "capabilityToken": handshake["capabilityToken"],
        "sourceId": session["sourceId"],
        "requestId": request_id,
        "method": method,
        "payload": payload,
    }


def test_sandbox_metadata_rejects_a_bundle_that_needs_the_host_module_graph() -> None:
    dependent = _plugin().model_copy(
        update={
            "client_bundle": DshClientBundle(
                digest=_BUNDLE_DIGEST,
                content_bytes=len(_BUNDLE),
                external=("react",),
                inject=(),
                compatible=True,
            )
        }
    )

    projected = api_plugin_routes._public_dsh_client_bundle(dependent)  # noqa: SLF001

    assert projected is not None
    assert projected["compatible"] is False
    assert projected["sandboxCompatible"] is False
    assert projected["executionMode"] == "deny"
    assert projected["url"] is None
    assert projected["sandboxBundleUrl"] is None
    assert "self-contained" in projected["sandboxIncompatibilityReason"]


@pytest.fixture
def studio_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _FakeCapabilities]:
    capabilities = _FakeCapabilities()
    sessions = DshUiSandboxSessionStore()
    service = StudioService(
        tmp_path,
        dsh_capability_service=capabilities,  # type: ignore[arg-type]
        dsh_ui_sessions=sessions,
    )
    monkeypatch.setattr(api_plugin_routes, "DshProfilePluginBridge", _FakeBridge)
    app = create_studio_app(
        tmp_path,
        service=service,
        session_token="studio-session",
        csrf_token="csrf-token",
    )
    return app, capabilities


@pytest.mark.asyncio
async def test_public_sandbox_routes_are_exact_anonymous_and_cookie_free(
    studio_app: tuple[Any, _FakeCapabilities],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _capabilities = studio_app
    descriptor = _descriptor()
    grant = app.state.studio_service.dsh_ui_sessions.create_session(
        plugin_id=_PLUGIN_ID,
        extension_id="dsh.ui.fixture",
        client_digest=_BUNDLE_DIGEST,
        descriptor_digest=descriptor.descriptor_digest,
        generation_id=_GENERATION_ID,
        parent_origin="http://testserver",
        allowed_tool_ids=("fixture.echo",),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        frame = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/frame",
            params={"uiSessionId": grant.session_id},
            headers={"Origin": "null", "Cookie": "private=value"},
        )
        assert frame.status_code == 200
        assert "X-Frame-Options" not in frame.headers
        assert "sandbox allow-scripts" in frame.headers["Content-Security-Policy"]
        assert "connect-src 'none'" in frame.headers["Content-Security-Policy"]
        assert "Set-Cookie" not in frame.headers
        assert grant.capability_token not in frame.text
        assert "private=value" not in frame.text

        bundle = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/client-bundle",
            params={"pluginName": _PLUGIN_ID, "digest": _BUNDLE_DIGEST},
            headers={"Origin": "null", "Cookie": "private=value"},
        )
        assert bundle.status_code == 200
        assert bundle.content == _BUNDLE
        assert bundle.headers["Access-Control-Allow-Origin"] == "*"
        assert bundle.headers["Cross-Origin-Resource-Policy"] == "cross-origin"
        assert bundle.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert "Set-Cookie" not in bundle.headers

        dependent = _plugin().model_copy(
            update={
                "client_bundle": _plugin().client_bundle.model_copy(update={"external": ("react",)})
            }
        )
        monkeypatch.setattr(_FakeBridge, "get_plugin", lambda _bridge, _name: dependent)
        rejected_bundle = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/client-bundle",
            params={"pluginName": _PLUGIN_ID, "digest": _BUNDLE_DIGEST},
            headers={"Origin": "null"},
        )
        assert rejected_bundle.status_code == 409
        assert rejected_bundle.json()["error"]["code"] == "DSH_UI_CLIENT_NOT_SELF_CONTAINED"

        disabled = _plugin().model_copy(update={"enabled": False})
        monkeypatch.setattr(_FakeBridge, "get_plugin", lambda _bridge, _name: disabled)
        disabled_bundle = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/client-bundle",
            params={"pluginName": _PLUGIN_ID, "digest": _BUNDLE_DIGEST},
            headers={"Origin": "null"},
        )
        assert disabled_bundle.status_code == 409
        assert disabled_bundle.json()["error"]["code"] == "DSH_UI_CLIENT_UNAVAILABLE"

        non_exact = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/frame/extra",
            headers={"Origin": "null"},
        )
        assert non_exact.status_code == 403
        protected = await client.get(
            "/api/v1/plugin-ecosystems/dsh/profile",
            headers={"Origin": "null"},
        )
        assert protected.status_code == 403


@pytest.mark.asyncio
async def test_ui_session_intersects_tools_and_relays_only_typed_capabilities(
    studio_app: tuple[Any, _FakeCapabilities],
) -> None:
    app, capabilities = studio_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/v1/plugin-ecosystems/dsh/ui-sessions",
            headers=_request_headers(write=True),
            json={
                "pluginId": _PLUGIN_ID,
                "clientDigest": _BUNDLE_DIGEST,
                "toolIds": ["fixture.echo", "admin.hidden"],
            },
        )
        assert created.status_code == 201, created.text
        session = created.json()
        assert [tool["name"] for tool in session["allowedTools"]] == ["fixture.echo"]
        assert [point["type"] for point in session["extensionPoints"]] == [
            "studio.sidebar.navigation",
            "studio.route",
            "studio.workspace.tab",
        ]
        encoded_session = json.dumps(session)
        assert "127.0.0.1:43123" not in encoded_session
        assert "lease-secret" not in encoded_session

        listed = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/ui-sessions/{session['uiSessionId']}/messages",
            headers=_request_headers(write=True),
            json={
                "sourceId": session["sourceId"],
                "frameOrigin": "null",
                "message": _request_message(
                    session,
                    request_id="request-list",
                    method="listTools",
                    payload={},
                ),
            },
        )
        assert listed.status_code == 200, listed.text
        assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["fixture.echo"]

        forbidden = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/ui-sessions/{session['uiSessionId']}/messages",
            headers=_request_headers(write=True),
            json={
                "sourceId": session["sourceId"],
                "frameOrigin": "null",
                "message": _request_message(
                    session,
                    request_id="request-forbidden",
                    method="callTool",
                    payload={
                        "callId": "forbidden-call",
                        "toolId": "admin.hidden",
                        "arguments": {},
                    },
                ),
            },
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "DSH_UI_TOOL_FORBIDDEN"

        call_request = {
            "sourceId": session["sourceId"],
            "frameOrigin": "null",
            "message": _request_message(
                session,
                request_id="request-call",
                method="callTool",
                payload={
                    "callId": "slow-call",
                    "toolId": "fixture.echo",
                    "arguments": {"wait": True},
                    "deadlineMs": 30_000,
                },
            ),
        }
        pending = asyncio.create_task(
            client.post(
                f"/api/v1/plugin-ecosystems/dsh/ui-sessions/{session['uiSessionId']}/messages",
                headers=_request_headers(write=True),
                json=call_request,
            )
        )
        await asyncio.wait_for(capabilities.call_started.wait(), timeout=1)
        cancelled = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/ui-sessions/{session['uiSessionId']}/messages",
            headers=_request_headers(write=True),
            json={
                "sourceId": session["sourceId"],
                "frameOrigin": "null",
                "message": _request_message(
                    session,
                    request_id="request-cancel",
                    method="cancelTool",
                    payload={"callId": "slow-call"},
                ),
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["result"] == {"cancelled": True}
        assert capabilities.cancelled
        capabilities.release_call.set()
        completed = await pending
        assert completed.status_code == 200
        assert completed.json()["result"]["isError"] is False


@pytest.mark.asyncio
async def test_same_descriptor_refresh_cannot_reuse_an_authorized_ui_message(
    studio_app: tuple[Any, _FakeCapabilities],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, capabilities = studio_app
    studio = app.state.studio_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/api/v1/plugin-ecosystems/dsh/ui-sessions",
            headers=_request_headers(write=True),
            json={
                "pluginId": _PLUGIN_ID,
                "clientDigest": _BUNDLE_DIGEST,
                "toolIds": ["fixture.echo"],
            },
        )
        assert created.status_code == 201
        session = created.json()
        authorize = studio.dsh_ui_sessions.authorize_message

        def authorize_then_refresh(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            authorized = authorize(*args, **kwargs)
            studio.dsh_ui_sessions.revoke_all()
            capabilities.generation_id = "dshgen_" + "n" * 32
            return authorized

        monkeypatch.setattr(
            studio.dsh_ui_sessions,
            "authorize_message",
            authorize_then_refresh,
        )
        rejected = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/ui-sessions/{session['uiSessionId']}/messages",
            headers=_request_headers(write=True),
            json={
                "sourceId": session["sourceId"],
                "frameOrigin": "null",
                "message": _request_message(
                    session,
                    request_id="stale-request",
                    method="callTool",
                    payload={
                        "callId": "stale-call",
                        "toolId": "fixture.echo",
                        "arguments": {},
                    },
                ),
            },
        )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "DSH_UI_DESCRIPTOR_CHANGED"
    assert capabilities.calls == []


@pytest.mark.asyncio
async def test_capability_metadata_has_no_lease_and_legacy_loader_is_denied(
    studio_app: tuple[Any, _FakeCapabilities],
) -> None:
    app, _capabilities = studio_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        capabilities = await client.get(
            "/api/v1/plugin-ecosystems/dsh/capabilities",
            headers=_request_headers(),
        )
        assert capabilities.status_code == 200
        encoded = json.dumps(capabilities.json())
        assert "endpoint" not in encoded.lower()
        assert "authorization" not in encoded.lower()
        assert "bearer" not in encoded.lower()
        bindable = capabilities.json()["bindableResource"]
        assert bindable["kind"] == "mcp"
        assert bindable["source"] == "provider"
        assert bindable["contract"]["materialization"] == "dsh-profile"
        assert (
            app.state.studio_service.catalog.get(bindable["resourceId"]).resource_id
            == bindable["resourceId"]
        )
        managed_probe = await client.post(
            f"/api/v1/catalog/mcp-servers/{bindable['resourceId']}:probe",
            headers=_request_headers(write=True),
        )
        assert managed_probe.status_code == 422
        assert managed_probe.json()["error"]["code"] == "DSH_MCP_MANAGED_RESOURCE_REQUIRED"
        raw_probe = await client.post(
            "/api/v1/mcp-servers:probe",
            headers=_request_headers(write=True),
            json={
                key: value
                for key, value in bindable["contract"].items()
                if key != "discoveredTools"
            },
        )
        assert raw_probe.status_code == 422
        assert raw_probe.json()["error"]["code"] == "DSH_MCP_MANAGED_RESOURCE_REQUIRED"

        profile = await client.get(
            "/api/v1/plugin-ecosystems/dsh/profile",
            headers=_request_headers(),
        )
        assert profile.status_code == 200, profile.text
        client_bundle = profile.json()["clientBundles"][0]
        assert client_bundle["compatible"] is False
        assert client_bundle["sandboxCompatible"] is True
        assert client_bundle["executionMode"] == "sandbox"
        assert client_bundle["url"] is None


@pytest.mark.asyncio
async def test_profile_mutation_revokes_sessions_and_cancels_active_calls(
    studio_app: tuple[Any, _FakeCapabilities],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, capabilities = studio_app
    studio = app.state.studio_service

    async def local_reconfigure(operation):  # type: ignore[no-untyped-def]
        await studio.reset_dsh_capability_state()
        return await operation()

    monkeypatch.setattr(studio, "reconfigure_dsh_profile", local_reconfigure)
    grant = studio.dsh_ui_sessions.create_session(
        plugin_id=_PLUGIN_ID,
        extension_id="dsh.ui.mutation",
        client_digest=_BUNDLE_DIGEST,
        descriptor_digest=capabilities.descriptor.descriptor_digest,
        generation_id=_GENERATION_ID,
        parent_origin="http://testserver",
        allowed_tool_ids=("fixture.echo",),
    )
    studio.dsh_ui_sessions.authorize_message(
        {
            "protocolVersion": "agentkit.dsh-ui/v1",
            "kind": "request",
            "sessionId": grant.session_id,
            "capabilityToken": grant.capability_token,
            "sourceId": grant.source_id,
            "requestId": "mutation-request",
            "method": "callTool",
            "payload": {
                "callId": "mutation-call",
                "toolId": "fixture.echo",
                "arguments": {},
            },
        },
        parent_origin="http://testserver",
        source_id=grant.source_id,
        frame_origin=DSH_UI_FRAME_ORIGIN,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        disabled = await client.post(
            f"/api/v1/plugin-ecosystems/dsh/plugins/{_PLUGIN_ID}:disable",
            headers=_request_headers(write=True),
        )
    assert disabled.status_code == 200, disabled.text
    with pytest.raises(DshUiSandboxError):
        studio.dsh_ui_sessions.frame_grant(grant.session_id)
    assert capabilities.cancelled == [dsh_ui_mcp_call_id(grant.session_id, "mutation-call")]
    assert capabilities.refresh_count == 1


@pytest.mark.asyncio
async def test_ui_route_purges_expired_sessions_and_cancels_their_calls(
    studio_app: tuple[Any, _FakeCapabilities],
) -> None:
    app, capabilities = studio_app
    studio = app.state.studio_service
    now = [0.0]
    studio.dsh_ui_sessions._clock = lambda: now[0]  # noqa: SLF001
    grant = studio.dsh_ui_sessions.create_session(
        plugin_id=_PLUGIN_ID,
        extension_id="dsh.ui.expiring",
        client_digest=_BUNDLE_DIGEST,
        descriptor_digest=capabilities.descriptor.descriptor_digest,
        generation_id=_GENERATION_ID,
        parent_origin="http://testserver",
        allowed_tool_ids=("fixture.echo",),
    )
    studio.dsh_ui_sessions.authorize_message(
        {
            "protocolVersion": "agentkit.dsh-ui/v1",
            "kind": "request",
            "sessionId": grant.session_id,
            "capabilityToken": grant.capability_token,
            "sourceId": grant.source_id,
            "requestId": "expiring-request",
            "method": "callTool",
            "payload": {
                "callId": "expiring-call",
                "toolId": "fixture.echo",
                "arguments": {},
            },
        },
        parent_origin="http://testserver",
        source_id=grant.source_id,
        frame_origin=DSH_UI_FRAME_ORIGIN,
    )
    now[0] = studio.dsh_ui_sessions.limits.session_ttl_seconds + 1

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        expired = await client.get(
            "/api/v1/plugin-ecosystems/dsh/sandbox/frame",
            params={"uiSessionId": grant.session_id},
            headers={"Origin": "null"},
        )

    assert expired.status_code == 403
    assert capabilities.cancelled == [dsh_ui_mcp_call_id(grant.session_id, "expiring-call")]
