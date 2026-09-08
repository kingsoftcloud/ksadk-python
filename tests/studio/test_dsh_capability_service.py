from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ksadk.plugins.bridges.dsh import DshProfileProjection
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.dsh_capabilities import (
    DSH_CAPABILITY_BUNDLE_PACKAGE,
    DSH_CAPABILITY_HOST_VERSION,
    DSH_CAPABILITY_MCP_PROTOCOL,
    DshCapabilityTool,
    DshMcpConnectorLease,
    DshProfileCapabilityDescriptor,
    DshProfileCapabilityInventory,
    _inventory_digest,
)
from ksadk.studio import dsh_capability_service as capability_module
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from ksadk.studio.errors import StudioError


def _tool() -> DshCapabilityTool:
    return DshCapabilityTool(
        name="fixture.echo",
        description="Echo one value",
        input_schema={"type": "object", "additionalProperties": False},
    )


def _descriptor() -> DshProfileCapabilityDescriptor:
    tool = _tool()
    return DshProfileCapabilityDescriptor(
        dsh_version="0.1.1-rc.2",
        profile="studio",
        profile_digest="sha256:" + "a" * 64,
        inventory_digest=_inventory_digest((tool,)),
        tools=(tool,),
    )


class _FakeHost:
    def __init__(self, descriptor: DshProfileCapabilityDescriptor) -> None:
        self.descriptor = descriptor
        self.disposed = False
        self.healthy = True
        self.lease_count = 0
        self.lease_value = DshMcpConnectorLease(
            endpoint="http://127.0.0.1:43123/mcp",
            profile=descriptor.profile,
            profile_digest=descriptor.profile_digest,
            descriptor_digest=descriptor.descriptor_digest,
            _bearer_token="lease-secret",
        )

    async def lease(self) -> DshMcpConnectorLease:
        self.lease_count += 1
        self.healthy = True
        return self.lease_value

    async def health(self) -> bool:
        return self.healthy

    async def inventory(self) -> DshProfileCapabilityInventory:
        return DshProfileCapabilityInventory(
            profile=self.descriptor.profile,
            profile_digest=self.descriptor.profile_digest,
            descriptor_digest=self.descriptor.descriptor_digest,
            inventory_digest=self.descriptor.inventory_digest,
            state="ready",
            pid=4321,
            tool_count=len(self.descriptor.tools),
            circuit_state="closed",
            consecutive_failures=0,
            retry_after_seconds=0,
        )

    async def dispose(self) -> None:
        self.disposed = True


class _RecordingService(StudioDshCapabilityService):
    def __init__(self, workspace: Path, **kwargs: Any) -> None:
        self.descriptor_value = _descriptor()
        self.hosts: list[_FakeHost] = []
        self.host_kwargs: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.call_started = asyncio.Event()
        self.release_call = asyncio.Event()

        def host_factory(*_args: Any, **host_kwargs: Any) -> _FakeHost:
            self.host_kwargs.append(host_kwargs)
            host = _FakeHost(self.descriptor_value)
            self.hosts.append(host)
            return host

        super().__init__(
            workspace,
            dsh_home=workspace / "dsh-home",
            dsh_command=("/pinned/dsh",),
            host_factory=host_factory,
            **kwargs,
        )

    def _resolve_command(self) -> tuple[str, ...]:
        return ("/pinned/dsh",)

    def _project_profile(self, command: tuple[str, ...]) -> DshProfileProjection:
        assert command == ("/pinned/dsh",)
        return DshProfileProjection(
            profile="studio",
            bundles=("@example/plugin",),
            config_digest=self.descriptor_value.profile_digest,
            config_bytes=3,
            host_version="0.1.1-rc.2",
        )

    async def _mcp_request(
        self,
        lease: DshMcpConnectorLease,
        *,
        request_id: str,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del lease, params, timeout_seconds
        self.requests.append((method, request_id))
        if method == "initialize":
            return {
                "protocolVersion": DSH_CAPABILITY_MCP_PROTOCOL,
                "serverInfo": {
                    "name": DSH_CAPABILITY_BUNDLE_PACKAGE,
                    "version": DSH_CAPABILITY_HOST_VERSION,
                },
                "capabilities": {"tools": {}},
            }
        if method == "tools/list":
            return {
                "tools": [
                    tool.model_dump(by_alias=True, mode="json")
                    for tool in self.descriptor_value.tools
                ]
            }
        self.call_started.set()
        await self.release_call.wait()
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    async def _send_notification(
        self,
        lease: DshMcpConnectorLease,
        *,
        method: str,
        params: dict[str, Any],
        tolerate_failure: bool,
    ) -> None:
        del lease, tolerate_failure
        self.notifications.append((method, dict(params)))


@pytest.mark.asyncio
async def test_service_initializes_once_and_exposes_only_public_facts(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path)

    descriptor = await service.describe()
    tools = await service.list_tools()
    inventory = await service.inventory()

    assert tools == descriptor.tools
    assert inventory.descriptor_digest == descriptor.descriptor_digest
    assert [method for method, _request_id in service.requests] == [
        "initialize",
        "tools/list",
    ]
    assert service.notifications == [("notifications/initialized", {})]
    assert len(service.hosts) == 1
    public = json.dumps(
        {
            "descriptor": descriptor.model_dump(by_alias=True, mode="json"),
            "inventory": inventory.model_dump(by_alias=True, mode="json"),
        }
    )
    assert "43123" not in public
    assert "lease-secret" not in public

    host = service.hosts[0]
    await service.aclose()
    assert host.disposed is True
    with pytest.raises(StudioError) as closed:
        await service.describe()
    assert closed.value.code == "DSH_CAPABILITY_SERVICE_CLOSED"


@pytest.mark.asyncio
async def test_call_is_descriptor_fenced_validated_and_cancelled_on_refresh(
    tmp_path: Path,
) -> None:
    service = _RecordingService(tmp_path)

    with pytest.raises(StudioError) as malformed:
        await service.call_tool(
            call_id="call-one",
            tool_name="bad/tool",
            arguments={},
            deadline_ms=1_000,
        )
    assert malformed.value.code == "DSH_CAPABILITY_TOOL_INVALID"
    assert service.hosts == []

    with pytest.raises(StudioError) as forbidden:
        await service.call_tool(
            call_id="call-two",
            tool_name="not.present",
            arguments={},
            deadline_ms=1_000,
        )
    assert forbidden.value.code == "DSH_CAPABILITY_TOOL_FORBIDDEN"

    pending = asyncio.create_task(
        service.call_tool(
            call_id="call-three",
            tool_name="fixture.echo",
            arguments={"value": "ok"},
            deadline_ms=10_000,
        )
    )
    await asyncio.wait_for(service.call_started.wait(), timeout=1)
    host = service.hosts[0]
    await service.refresh()

    assert host.disposed is True
    assert (
        "notifications/cancelled",
        {"requestId": "call-three", "reason": "Studio UI cancelled the call"},
    ) in service.notifications
    service.release_call.set()
    assert (await pending)["isError"] is False

    await service.describe()
    assert len(service.hosts) == 2


@pytest.mark.asyncio
async def test_unhealthy_cached_generation_restarts_the_same_host(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path)
    await service.describe()
    host = service.hosts[0]
    host.healthy = False

    await service.describe()

    assert service.hosts == [host]
    assert host.lease_count == 2
    assert [method for method, _request_id in service.requests] == [
        "initialize",
        "initialize",
    ]


@pytest.mark.asyncio
async def test_same_descriptor_refresh_rejects_an_old_ui_generation(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path)
    descriptor, generation_id = await service.descriptor_generation()
    await service.refresh()

    with pytest.raises(StudioError) as stale:
        await service.call_tool(
            call_id="stale-generation-call",
            tool_name="fixture.echo",
            arguments={"value": "must-not-run"},
            deadline_ms=1_000,
            expected_descriptor_digest=descriptor.descriptor_digest,
            expected_generation_id=generation_id,
        )

    assert stale.value.code == "DSH_CAPABILITY_GENERATION_CHANGED"
    assert service.call_started.is_set() is False


@pytest.mark.asyncio
async def test_refresh_defers_cancellation_until_the_generation_is_disposed(
    tmp_path: Path,
) -> None:
    dispose_started = asyncio.Event()
    release_dispose = asyncio.Event()

    class BlockingDisposeHost(_FakeHost):
        async def dispose(self) -> None:
            dispose_started.set()
            await release_dispose.wait()
            await super().dispose()

    service = _RecordingService(tmp_path)
    host = BlockingDisposeHost(service.descriptor_value)
    service._host = host  # noqa: SLF001

    refresh = asyncio.create_task(service.refresh())
    await asyncio.wait_for(dispose_started.wait(), timeout=1)
    refresh.cancel()
    await asyncio.sleep(0)

    assert refresh.done() is False
    assert service._host is None  # noqa: SLF001

    release_dispose.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert host.disposed is True


@pytest.mark.asyncio
async def test_start_failures_keep_one_host_and_its_circuit_history(tmp_path: Path) -> None:
    descriptor = _descriptor()

    class FailingHost(_FakeHost):
        async def lease(self) -> DshMcpConnectorLease:
            self.lease_count += 1
            code = (
                "dsh_capability_start_failed"
                if self.lease_count < 3
                else "dsh_capability_circuit_open"
            )
            raise PluginHostError(code, "fixture failure")

    hosts: list[FailingHost] = []

    def host_factory(*_args: Any, **_kwargs: Any) -> FailingHost:
        host = FailingHost(descriptor)
        hosts.append(host)
        return host

    service = StudioDshCapabilityService(
        tmp_path,
        dsh_home=tmp_path / "dsh-home",
        dsh_command=("/pinned/dsh",),
        host_factory=host_factory,
    )
    service._resolve_command = lambda: ("/pinned/dsh",)  # type: ignore[method-assign]
    service._project_profile = lambda _command: DshProfileProjection(  # type: ignore[method-assign]
        profile="studio",
        bundles=("@example/plugin",),
        config_digest=descriptor.profile_digest,
        config_bytes=3,
        host_version="0.1.1-rc.2",
    )

    reasons: list[str | None] = []
    for _attempt in range(3):
        with pytest.raises(StudioError) as rejected:
            await service.describe()
        reasons.append(rejected.value.details.get("reason"))

    assert len(hosts) == 1
    assert hosts[0].lease_count == 3
    assert reasons == [
        "dsh_capability_start_failed",
        "dsh_capability_start_failed",
        "dsh_capability_circuit_open",
    ]


def test_command_resolution_always_uses_exact_version_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplied: list[str | None] = []

    def require_command(_manager: object, explicit: str | None = None) -> tuple[str, ...]:
        supplied.append(explicit)
        return ("/verified/dsh",)

    monkeypatch.setattr(
        capability_module.DshToolchainManager,
        "require_command",
        require_command,
    )
    service = StudioDshCapabilityService(
        tmp_path,
        dsh_home=tmp_path / "dsh-home",
        dsh_command=("/requested/dsh",),
    )

    assert service._resolve_command() == ("/verified/dsh",)  # noqa: SLF001
    assert supplied == ["/requested/dsh"]


@pytest.mark.asyncio
async def test_initialize_mismatch_disposes_the_unusable_generation(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path)

    async def invalid_initialize(
        _lease: DshMcpConnectorLease,
        *,
        request_id: str,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del request_id, params, timeout_seconds
        assert method == "initialize"
        return {
            "protocolVersion": "unexpected",
            "serverInfo": {
                "name": DSH_CAPABILITY_BUNDLE_PACKAGE,
                "version": DSH_CAPABILITY_HOST_VERSION,
            },
            "capabilities": {"tools": {}},
        }

    service._mcp_request = invalid_initialize  # type: ignore[method-assign]
    with pytest.raises(StudioError) as rejected:
        await service.describe()
    assert rejected.value.code == "DSH_CAPABILITY_PROTOCOL_INVALID"
    assert service.hosts[0].disposed is True


class _ResponseClient:
    response_content: bytes = b""

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_ResponseClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, _endpoint: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, content=self.response_content)


@pytest.mark.asyncio
async def test_result_limit_reserves_json_rpc_envelope_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_limit = 1_000
    service = StudioDshCapabilityService(
        tmp_path,
        dsh_home=tmp_path / "dsh-home",
        max_response_bytes=result_limit,
    )
    lease = _FakeHost(_descriptor()).lease_value
    monkeypatch.setattr(capability_module.httpx, "AsyncClient", _ResponseClient)

    within_result_limit = {"value": "x" * 950}
    _ResponseClient.response_content = json.dumps(
        {"jsonrpc": "2.0", "id": "boundary", "result": within_result_limit},
        separators=(",", ":"),
    ).encode()
    assert len(_ResponseClient.response_content) > result_limit
    assert (
        await service._mcp_request(  # noqa: SLF001
            lease,
            request_id="boundary",
            method="tools/call",
            params={},
            timeout_seconds=1,
        )
    ) == within_result_limit

    oversized_result = {"value": "x" * result_limit}
    _ResponseClient.response_content = json.dumps(
        {"jsonrpc": "2.0", "id": "boundary", "result": oversized_result},
        separators=(",", ":"),
    ).encode()
    with pytest.raises(StudioError) as rejected:
        await service._mcp_request(  # noqa: SLF001
            lease,
            request_id="boundary",
            method="tools/call",
            params={},
            timeout_seconds=1,
        )
    assert rejected.value.code == "DSH_CAPABILITY_PROTOCOL_INVALID"

    service_with_host = _RecordingService(tmp_path / "host", max_response_bytes=result_limit)
    await service_with_host.describe()
    assert service_with_host.host_kwargs[0]["max_result_bytes"] == result_limit
    assert service_with_host._max_wire_response_bytes == result_limit + 16 * 1024  # noqa: SLF001
