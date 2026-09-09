from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot, DshProfileProjection
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
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream


def _tool() -> DshCapabilityTool:
    return DshCapabilityTool(
        name="fixture.echo",
        description="Echo one value",
        input_schema={"type": "object", "additionalProperties": False},
    )


@pytest.mark.parametrize("action", ["refresh", "close", "restart"])
async def test_resource_workers_are_closed_with_core_generation(
    tmp_path: Path, action: str
) -> None:
    service = _RecordingService(tmp_path)
    await service.inventory()
    closed = []

    class Resources:
        async def aclose(self):
            closed.append(True)

    service._resource_supervisor = Resources()
    try:
        if action == "refresh":
            await service.refresh()
        elif action == "close":
            await service.aclose()
        else:
            service.hosts[0].healthy = False
            await service.inventory()
        assert closed == [True]
        assert service._resource_supervisor is None
    finally:
        await service.aclose()


async def test_resource_cleanup_failure_still_disposes_core(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path)
    await service.inventory()

    class Resources:
        async def aclose(self):
            raise RuntimeError("fixture cleanup failed")

    service._resource_supervisor = Resources()
    with pytest.raises(RuntimeError, match="fixture cleanup failed"):
        await service.aclose()
    assert service.hosts[0].disposed is True


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

    async def configure_resource_socket(self, path):
        assert path.is_socket()
        self.resource_socket = path

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
async def test_resource_service_does_not_mount_studio_web_app(tmp_path: Path) -> None:
    service = _RecordingService(tmp_path, mount_studio_app=False)

    await service.inventory()

    assert service.host_kwargs[0]["studio_index"] is None
    await service.aclose()


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


def _resource_snapshot(service):
    return DshProfileBuildSnapshot(
        projection=service._project_profile(("/pinned/dsh",)),
        dependency_lock_digest="sha256:" + "b" * 64,
        installation_digest="sha256:" + "c" * 64,
    )


def _bind_resource_profile(payload, expected):
    from ksadk.resource_runtime.snapshots import ResourceSnapshot
    from ksadk.resource_runtime.worker import WorkerInitialization

    payload["resourceSnapshot"]["dshProfile"] = expected.model_dump(by_alias=True)
    digest = ResourceSnapshot.model_validate(payload["resourceSnapshot"]).digest
    for scope in payload["scopes"]:
        scope["bindingSnapshotDigest"] = digest
    return WorkerInitialization.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_resource_activation_approval_and_receipt_survive_service_restart(
    tmp_path, monkeypatch, memory_upstream, authorized,
):
    from tests.resource_runtime.test_activation_approvals import (
        Approval,
        request,
        write_initialization,
    )

    endpoint, calls = memory_upstream
    operation_id = None
    for _ in range(2):
        service = _RecordingService(tmp_path)
        expected = _resource_snapshot(service)
        monkeypatch.setattr(service, "_verify_resource_build_snapshot", lambda *args: None)
        try:
            descriptor, generation = await service.prepare_resource_generation(expected)
            payload = write_initialization(endpoint).pipe_payload()
            payload["scopes"][0]["generationId"] = generation
            payload["scopes"][0]["profileDigest"] = descriptor.profile_digest
            active = await service.activate_resources(
                _bind_resource_profile(payload, expected), expected=expected,
                write_authorizer=Approval("user-a") if authorized else None,
            )
            reply = await service._resource_supervisor._broker.dispatch(request(active))
            if authorized:
                assert reply["result"]["status"] == "accepted_pending"
                current = reply["result"]["operationId"]
                assert operation_id is None or current == operation_id
                operation_id = current
            else:
                assert reply["error"]["code"] == "RESOURCE_APPROVAL_REQUIRED"
        finally:
            await service.aclose()
    assert len(calls) == (1 if authorized else 0)


@pytest.mark.asyncio
async def test_resource_generation_preparation_and_actual_worker_lifetime(tmp_path, monkeypatch):
    from tests.resource_runtime.test_worker_process import initialization

    service = _RecordingService(tmp_path)
    expected = _resource_snapshot(service)
    checks = []
    monkeypatch.setattr(
        service,
        "_verify_resource_build_snapshot",
        lambda command, snapshot: checks.append(snapshot),
    )
    try:
        descriptor, generation = await service.prepare_resource_generation(expected)
        assert checks == [expected, expected]
        payload = initialization("https://resources.example.test").pipe_payload()
        payload["scopes"][0]["generationId"] = generation
        payload["scopes"][0]["profileDigest"] = descriptor.profile_digest
        active = await service.activate_resources(
            _bind_resource_profile(payload, expected), expected=expected
        )
        assert active.worker_pid > 0
        assert active.socket_path.exists()
        assert len(checks) == 3
        async def revalidate(scopes):
            assert scopes == (active.leases[0].scope,)
            return True

        renewed = await service.renew_resources(active, expected=expected, revalidate=revalidate)
        assert len(checks) == 4
        assert renewed.worker_pid == active.worker_pid
        assert renewed.leases[0].handle != active.leases[0].handle
        await service.aclose()
        assert not active.socket_path.exists()
        assert service._resource_generation_snapshot is None
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_resource_preparation_restarts_unattested_same_profile_core(
    tmp_path, monkeypatch
):
    service = _RecordingService(tmp_path)
    await service.describe()
    original = service.hosts[0]
    expected = _resource_snapshot(service)
    checks = []
    monkeypatch.setattr(
        service,
        "_verify_resource_build_snapshot",
        lambda command, snapshot: checks.append(snapshot),
    )

    _descriptor, generation = await service.prepare_resource_generation(expected)

    assert original.disposed
    assert len(service.hosts) == 2
    assert checks == [expected, expected]
    assert service._resource_generation_snapshot == expected
    assert generation == service._generation_id
    await service.aclose()


@pytest.mark.asyncio
async def test_resource_preparation_does_not_replace_incompatible_active_core(tmp_path):
    service = _RecordingService(tmp_path)
    await service.describe()
    host = service.hosts[0]
    expected = _resource_snapshot(service)
    expected = expected.model_copy(
        update={
            "projection": expected.projection.model_copy(
                update={"config_digest": "sha256:" + "d" * 64}
            )
        }
    )

    with pytest.raises(StudioError) as rejected:
        await service.prepare_resource_generation(expected)

    assert rejected.value.code == "RESOURCE_PROFILE_IN_USE"
    assert not host.disposed
    await service.aclose()


@pytest.mark.asyncio
async def test_changed_installation_revokes_prepared_generation_before_worker(
    tmp_path, monkeypatch
):
    from tests.resource_runtime.test_worker_process import initialization

    service = _RecordingService(tmp_path)
    expected = _resource_snapshot(service)
    monkeypatch.setattr(service, "_verify_resource_build_snapshot", lambda *args: None)
    await service.prepare_resource_generation(expected)
    host = service.hosts[0]

    def reject(*args):
        raise StudioError(
            "RESOURCE_BUILD_INSTALLATION_MISMATCH", "fixture changed", status_code=409
        )

    monkeypatch.setattr(service, "_verify_resource_build_snapshot", reject)
    with pytest.raises(StudioError) as rejected:
        await service.activate_resources(
            _bind_resource_profile(
                initialization("https://resources.example.test").pipe_payload(), expected
            ), expected=expected,
        )
    assert rejected.value.code == "RESOURCE_BUILD_INSTALLATION_MISMATCH"
    assert host.disposed
    assert service._resource_supervisor is None
    assert service._resource_generation_snapshot is None


@pytest.mark.asyncio
async def test_resource_activation_requires_prepared_build(tmp_path):
    from tests.resource_runtime.test_worker_process import initialization

    service = _RecordingService(tmp_path)
    with pytest.raises(StudioError) as rejected:
        await service.activate_resources(
            initialization("https://resources.example.test"), expected=_resource_snapshot(service)
        )
    assert rejected.value.code == "RESOURCE_BUILD_NOT_PREPARED"
    assert not service.hosts
    assert service._resource_supervisor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "config", "lock", "installation"])
async def test_resource_activation_cannot_mix_build_and_core_snapshots(
    tmp_path, monkeypatch, change,
):
    from ksadk.resource_runtime.worker import WorkerInitialization
    from tests.resource_runtime.test_worker_process import initialization

    service = _RecordingService(tmp_path)
    expected = _resource_snapshot(service)
    monkeypatch.setattr(service, "_verify_resource_build_snapshot", lambda *args: None)
    try:
        descriptor, generation = await service.prepare_resource_generation(expected)
        payload = initialization("https://resources.example.test").pipe_payload()
        payload["scopes"][0]["generationId"] = generation
        payload["scopes"][0]["profileDigest"] = descriptor.profile_digest
        admitted = _bind_resource_profile(payload, expected)
        active = await service.activate_resources(admitted, expected=expected)
        supervisor = service._resource_supervisor
        altered = expected.model_dump(by_alias=True)
        if change == "config":
            altered["projection"]["configDigest"] = "sha256:" + "d" * 64
        elif change == "lock":
            altered["dependencyLockDigest"] = "sha256:" + "d" * 64
        elif change == "installation":
            altered["installationDigest"] = "sha256:" + "d" * 64
        payload["scopes"][0]["activationId"] = "different-activation"
        candidate = _bind_resource_profile(
            payload, DshProfileBuildSnapshot.model_validate(altered)
        )
        if change == "missing":
            from ksadk.resource_runtime.snapshots import ResourceSnapshot

            payload = candidate.pipe_payload()
            payload["resourceSnapshot"].pop("dshProfile")
            payload["scopes"][0]["bindingSnapshotDigest"] = ResourceSnapshot.model_validate(
                payload["resourceSnapshot"]
            ).digest
            candidate = WorkerInitialization.model_validate(payload)
        with pytest.raises(StudioError) as rejected:
            await service.activate_resources(candidate, expected=expected)
        assert rejected.value.code == "RESOURCE_BUILD_PROFILE_MISMATCH"
        assert service._resource_supervisor is supervisor
        assert active.socket_path.exists()
        assert not service.hosts[0].disposed
        assert service._resource_generation_snapshot == expected
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["lease", "initialize"])
async def test_resource_core_startup_failure_cleans_socket_and_can_retry_same_build(
    tmp_path, monkeypatch, stage,
):
    class ResourceService(_RecordingService):
        def _project_profile(self, command):
            return super()._project_profile(command).model_copy(update={
                "bundles": ("@kingsoftcloud/dsh-platform-resources",),
            })

    service = ResourceService(tmp_path)
    expected = _resource_snapshot(service)
    monkeypatch.setattr(service, "_verify_resource_build_snapshot", lambda *args: None)
    paths = []
    original = _FakeHost.lease
    initialize = service._initialize_lease
    fail = [True]

    async def lease(host):
        paths.append(host.resource_socket)
        assert not service._resource_supervisor._running
        assert not service._resource_supervisor._registry._leases
        if fail[0] and stage == "lease":
            raise PluginHostError("fixture_start_failed", "fixture")
        return await original(host)

    async def initialize_lease(value):
        if fail[0] and stage == "initialize":
            raise StudioError("FIXTURE_INITIALIZE_FAILED", "fixture", status_code=503)
        return await initialize(value)

    monkeypatch.setattr(_FakeHost, "lease", lease)
    monkeypatch.setattr(service, "_initialize_lease", initialize_lease)
    try:
        with pytest.raises(StudioError):
            await service.prepare_resource_generation(expected)
        assert service._resource_supervisor is None
        assert not paths[0].exists()
        fail[0] = False
        _, generation = await service.prepare_resource_generation(expected)
        assert len(service.hosts) == 1  # retain the original host/circuit owner
        assert paths[1].is_socket() and paths[0] != paths[1]
        assert service._resource_supervisor.generation_id == generation
        assert service._resource_generation_snapshot == expected
    finally:
        await service.aclose()
    assert not paths[-1].exists()
