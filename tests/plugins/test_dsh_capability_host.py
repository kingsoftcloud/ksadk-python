from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections import deque
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ksadk.plugins.bridges.dsh import DshProfileProjection
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.dsh import DSH_AGENT_PROVIDER_HOST_METHODS
from ksadk.plugins.providers.dsh_capabilities import (
    DSH_CAPABILITY_BUNDLE_PACKAGE,
    DshCapabilityTool,
    DshProfileCapabilityDescriptor,
    DshProfileCapabilityHost,
    DshProfileCapabilityReady,
    _inventory_digest,
    load_dsh_capability_bundle,
)


def _fixture(name: str) -> Path:
    return Path(__file__).parents[1] / "fixtures" / name


def _projection(config: bytes = b"[]\n") -> DshProfileProjection:
    return DshProfileProjection(
        profile="fixture",
        bundles=("@ksadk-test/dsh-node-tool-plugin",),
        config_digest=f"sha256:{hashlib.sha256(config).hexdigest()}",
        config_bytes=len(config),
        host_version="0.1.1-rc.2",
    )


def _host(
    tmp_path: Path,
    *,
    config: bytes = b"[]\n",
    environment: dict[str, str] | None = None,
) -> DshProfileCapabilityHost:
    node = shutil.which("node")
    assert node is not None
    return DshProfileCapabilityHost(
        (node, str(_fixture("dsh-capability-fake-profile.mjs"))),
        projection=_projection(config),
        dsh_home=tmp_path / "dsh-home",
        cwd=tmp_path,
        environment={"KSADK_DSH_FAKE_CONFIG": config.decode(), **(environment or {})},
        node_command=node,
        startup_timeout=5,
        health_timeout=2,
        call_timeout=0.5,
    )


def test_capability_contract_fixture_and_python_models_agree() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads(
        (root / "contracts/plugin/v1/dsh-profile-capability-host.schema.json").read_text()
    )
    fixture = json.loads(
        (root / "contracts/plugin/v1/fixtures/dsh-profile-capability-host.json").read_text()
    )
    Draft202012Validator(schema).validate(fixture)
    ready = DshProfileCapabilityReady.model_validate(fixture["ready"])
    descriptor = DshProfileCapabilityDescriptor.model_validate(fixture["descriptor"])
    assert ready.inventory_digest == descriptor.inventory_digest
    assert descriptor.descriptor_digest == fixture["descriptorDigest"]
    assert "endpoint" not in descriptor.model_dump(by_alias=True, mode="json")


@pytest.mark.asyncio
async def test_capability_host_fails_closed_without_process_group_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ksadk.plugins.providers.dsh_capabilities._PROCESS_GROUP_ISOLATION_AVAILABLE",
        False,
    )
    host = _host(tmp_path)

    with pytest.raises(PluginHostError) as raised:
        await host.start()

    assert raised.value.code == "dsh_capability_platform_unsupported"
    assert host.pid is None


def test_capability_bundle_is_standard_and_legacy_provider_abi_is_unchanged() -> None:
    bundle = load_dsh_capability_bundle()
    package = json.loads((bundle.root / "package.json").read_text())
    assert bundle.package_name == DSH_CAPABILITY_BUNDLE_PACKAGE
    assert package["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert "./provider-host" not in package["exports"]
    assert DSH_AGENT_PROVIDER_HOST_METHODS == {
        "handshake",
        "describe",
        "preflight",
        "activate",
        "inventory",
        "execute",
        "cancel",
        "health",
        "drain",
        "dispose",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_padding", ["🙂" * 1024, "中文" * 20_000])
async def test_readiness_stdout_parser_handles_large_utf8_lines(
    tmp_path: Path,
    schema_padding: str,
) -> None:
    tool = DshCapabilityTool(
        name="fixture_echo",
        description="描述🙂",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string", "description": schema_padding}},
        },
    )
    inventory_digest = _inventory_digest((tool,))
    ready = DshProfileCapabilityReady(
        protocol_version="ksadk.dsh-capability-host/v1",
        host_version="1.0.0",
        dsh_version="0.1.1-rc.2",
        profile="fixture",
        profile_digest=_projection().config_digest,
        definition="mcp.connector/v1",
        transport="streamable-http",
        endpoint="http://127.0.0.1:43123/mcp",
        inventory_digest=inventory_digest,
        tools=(tool,),
    )
    line = (
        "@@KSADK_DSH_CAPABILITY_READY@@"
        + json.dumps(
            ready.model_dump(by_alias=True, mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    assert len(line) > 4096
    reader = asyncio.StreamReader()
    # Deliberately split within UTF-8 code points as well as the old 4 KiB boundary.
    for offset in range(0, len(line), 4093):
        reader.feed_data(line[offset : offset + 4093])
    reader.feed_eof()
    future = asyncio.get_running_loop().create_future()
    host = _host(tmp_path)

    await host._capture_stream(  # noqa: SLF001
        reader,
        deque(maxlen=64),
        ready_future=future,
    )

    assert future.result() == ready


@pytest.mark.asyncio
async def test_inventory_digest_and_order_are_cross_language_stable(tmp_path: Path) -> None:
    names = ["z_lower", "A_upper", "_under", ":colon", ".dot", "-dash", "a_lower"]
    schemas = [
        {
            "name": name,
            "description": (
                "x" * 1023 + "🙂" + "truncated" if name == "z_lower" else "stable"
            ),
            "parameters": {
                "type": "number",
                "minimum": 1e-7,
                "examples": [1e-6, 1e-5, 1e20, 1e21, -0.0],
                # These keys have a different UTF-16 versus UTF-8 ordering.
                "x-key-order": {"𐀀": True, "\ue000": False},
            },
        }
        for name in names
    ]
    host = _host(
        tmp_path,
        environment={
            "KSADK_DSH_FAKE_SCHEMAS_JSON": json.dumps(
                schemas,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
    )
    try:
        await host.start()
        assert [tool.name for tool in host.descriptor.tools] == sorted(names)
        by_name = {tool.name: tool for tool in host.descriptor.tools}
        assert by_name["z_lower"].description == "x" * 1023 + "🙂"
    finally:
        await host.dispose()


def test_tool_schema_rejects_non_scalar_unicode() -> None:
    with pytest.raises(ValueError, match="Unicode scalar"):
        DshCapabilityTool(
            name="invalid_unicode",
            description="ok",
            input_schema={"description": "\ud800"},
        )


@pytest.mark.asyncio
async def test_profile_host_exposes_authenticated_standard_mcp_and_cancels(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    try:
        lease = await host.start()
        assert lease.definition == "mcp.connector/v1"
        assert lease.transport == "streamable-http"
        assert "Bearer" not in repr(lease)
        assert "127.0.0.1" not in repr(lease)
        assert host.descriptor.tools[0].name == "fixture_echo"
        assert host._runtime_dir is not None  # noqa: SLF001
        assert not (host._runtime_dir / "ready.json").exists()  # noqa: SLF001
        assert all("KSADK_DSH_CAPABILITY_READY" not in line for line in host.stdout_tail)
        assert all("127.0.0.1" not in line for line in host.stdout_tail)

        async with httpx.AsyncClient(trust_env=False) as unauthorized:
            health_endpoint = lease.endpoint.replace("/mcp", "/health")
            assert (await unauthorized.get(health_endpoint)).status_code == 401

        aliases = {"agent_fixture_echo": "fixture_echo"}
        scoped_token_a = lease.bearer_token_for_runtime(aliases)
        scoped_token_b = lease.bearer_token_for_runtime(aliases)
        assert scoped_token_a != scoped_token_b
        scoped_headers_a = {
            "Authorization": f"Bearer {scoped_token_a}",
            "Content-Type": "application/json",
        }
        scoped_headers_b = {
            "Authorization": f"Bearer {scoped_token_b}",
            "Content-Type": "application/json",
        }
        async with (
            httpx.AsyncClient(
                headers=scoped_headers_a, timeout=3, trust_env=False
            ) as scoped_a,
            httpx.AsyncClient(
                headers=scoped_headers_b, timeout=3, trust_env=False
            ) as scoped_b,
        ):
            listed = await scoped_a.post(
                lease.endpoint,
                json={"jsonrpc": "2.0", "id": "scoped-list", "method": "tools/list"},
            )
            assert [item["name"] for item in listed.json()["result"]["tools"]] == [
                "agent_fixture_echo"
            ]
            forbidden = await scoped_a.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "scoped-forbidden",
                    "method": "tools/call",
                    "params": {"name": "fixture_echo", "arguments": {"message": "no"}},
                },
            )
            assert forbidden.json()["error"]["code"] == -32003
            allowed = await scoped_a.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "scoped-allowed",
                    "method": "tools/call",
                    "params": {
                        "name": "agent_fixture_echo",
                        "arguments": {"message": "scoped"},
                    },
                },
            )
            assert allowed.json()["result"]["structuredContent"]["message"] == "scoped"

            pending_scope_a = asyncio.create_task(
                scoped_a.post(
                    lease.endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": "shared-scoped-call",
                        "method": "tools/call",
                        "params": {
                            "name": "agent_fixture_echo",
                            "arguments": {"message": "slow-a", "delayMs": 10_000},
                        },
                    },
                )
            )
            pending_scope_b = asyncio.create_task(
                scoped_b.post(
                    lease.endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": "shared-scoped-call",
                        "method": "tools/call",
                        "params": {
                            "name": "agent_fixture_echo",
                            "arguments": {"message": "slow", "delayMs": 10_000},
                        },
                    },
                )
            )
            await asyncio.sleep(0.05)
            await scoped_a.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "shared-scoped-call"},
                },
            )
            scoped_a_cancelled = (await pending_scope_a).json()["result"]
            assert scoped_a_cancelled["_meta"]["io.ksadk/dsh"]["code"] == "ABORTED"
            await asyncio.sleep(0.05)
            assert not pending_scope_b.done()
            await scoped_b.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "shared-scoped-call"},
                },
            )
            scoped_cancelled = (await pending_scope_b).json()["result"]
            assert scoped_cancelled["_meta"]["io.ksadk/dsh"]["code"] == "ABORTED"

        async with httpx.AsyncClient(
            headers={**lease.headers(), "Content-Type": "application/json"},
            timeout=3,
            trust_env=False,
        ) as root_client:
            revoked = await root_client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "revoke-scope-a",
                    "method": "io.ksadk/scopes/revoke",
                    "params": {"token": scoped_token_a},
                },
            )
            assert revoked.json()["result"] == {"revoked": True}
        async with httpx.AsyncClient(
            headers=scoped_headers_a, timeout=3, trust_env=False
        ) as revoked_client:
            assert (
                await revoked_client.post(
                    lease.endpoint,
                    json={"jsonrpc": "2.0", "id": "after-revoke", "method": "tools/list"},
                )
            ).status_code == 401

        async with httpx.AsyncClient(
            headers=lease.headers(), timeout=3, trust_env=False
        ) as mcp_client:
            async with streamable_http_client(
                lease.endpoint,
                http_client=mcp_client,
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert initialized.protocolVersion == "2025-06-18"
                    listed = await session.list_tools()
                    assert [tool.name for tool in listed.tools] == ["fixture_echo"]
                    called = await session.call_tool("fixture_echo", {"message": "through-mcp"})
                    assert called.isError is False
                    assert called.content[0].text == "through-mcp"
                    assert called.structuredContent["message"] == "through-mcp"

        headers = {**lease.headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(headers=headers, timeout=3, trust_env=False) as client:
            pending = asyncio.create_task(
                client.post(
                    lease.endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": "slow-call",
                        "method": "tools/call",
                        "params": {
                            "name": "fixture_echo",
                            "arguments": {"message": "slow", "delayMs": 10_000},
                        },
                    },
                )
            )
            await asyncio.sleep(0.05)
            cancelled = await client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "slow-call", "reason": "test"},
                },
            )
            assert cancelled.status_code == 202
            result = (await pending).json()["result"]
            assert result["isError"] is True
            assert result["_meta"]["io.ksadk/dsh"]["code"] == "ABORTED"

            started = asyncio.get_running_loop().time()
            deadline = await client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "deadline-call",
                    "method": "tools/call",
                    "params": {
                        "name": "fixture_echo",
                        "arguments": {"message": "slow", "delayMs": 10_000},
                    },
                },
            )
            deadline_result = deadline.json()["result"]
            assert deadline_result["isError"] is True
            assert deadline_result["_meta"]["io.ksadk/dsh"]["code"] == "DEADLINE_EXCEEDED"
            assert asyncio.get_running_loop().time() - started < 2

            oversized_arguments = await client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "large-arguments",
                    "method": "tools/call",
                    "params": {
                        "name": "fixture_echo",
                        "arguments": {"message": "x" * 1500},
                    },
                },
            )
            assert oversized_arguments.json()["error"]["code"] == -32602
            oversized_result = await client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "large-result",
                    "method": "tools/call",
                    "params": {
                        "name": "fixture_echo",
                        "arguments": {"message": "large"},
                    },
                },
            )
            bounded = oversized_result.json()["result"]
            assert bounded["isError"] is True
            assert bounded["_meta"]["io.ksadk/dsh"]["code"] == "DSH_RESULT_TOO_LARGE"

        assert await host.health() is True
        inventory = await host.inventory()
        assert inventory.state == "ready"
        assert inventory.tool_count == 1
        assert inventory.pid == host.pid
    finally:
        await host.dispose()
    assert host.pid is None
    assert (await host.inventory()).state == "disposed"


@pytest.mark.asyncio
async def test_tool_that_ignores_abort_crashes_generation_and_recovers_capacity(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    try:
        lease = await host.start()
        first_pid = host.pid
        assert first_pid is not None
        headers = {**lease.headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            headers=headers, timeout=3, trust_env=False
        ) as client:
            response = await client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "ignore-abort",
                    "method": "tools/call",
                    "params": {
                        "name": "fixture_echo",
                        "arguments": {"message": "ignore-signal"},
                    },
                },
            )
        assert response.json()["result"]["_meta"]["io.ksadk/dsh"]["code"] == (
            "DEADLINE_EXCEEDED"
        )

        for _ in range(60):
            if host.pid is None:
                break
            await asyncio.sleep(0.05)
        assert host.pid is None
        assert await host.health() is False

        replacement = await host.start()
        assert host.pid is not None and host.pid != first_pid
        async with httpx.AsyncClient(
            headers={**replacement.headers(), "Content-Type": "application/json"},
            timeout=3,
            trust_env=False,
        ) as client:
            recovered = await client.post(
                replacement.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "after-restart",
                    "method": "tools/call",
                    "params": {
                        "name": "fixture_echo",
                        "arguments": {"message": "recovered"},
                    },
                },
            )
        assert recovered.json()["result"]["structuredContent"]["message"] == "recovered"
    finally:
        await host.dispose()


@pytest.mark.asyncio
async def test_scoped_revoke_fences_a_request_with_a_slow_body(tmp_path: Path) -> None:
    host = _host(tmp_path)
    try:
        lease = await host.start()
        token = lease.bearer_token_for_runtime(
            {"agent_fixture_echo": "fixture_echo"}
        )
        endpoint = httpx.URL(lease.endpoint)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "slow-body",
                "method": "tools/call",
                "params": {
                    "name": "agent_fixture_echo",
                    "arguments": {"message": "must-not-run"},
                },
            },
            separators=(",", ":"),
        ).encode()
        reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
        split = len(body) // 2
        headers = (
            f"POST {endpoint.raw_path.decode()} HTTP/1.1\r\n"
            f"Host: {endpoint.host}:{endpoint.port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(headers + body[:split])
        await writer.drain()
        await asyncio.sleep(0.05)

        async with httpx.AsyncClient(
            headers={**lease.headers(), "Content-Type": "application/json"},
            timeout=3,
            trust_env=False,
        ) as root_client:
            revoked = await root_client.post(
                lease.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "revoke-slow-body",
                    "method": "io.ksadk/scopes/revoke",
                    "params": {"token": token},
                },
            )
            assert revoked.json()["result"] == {"revoked": True}

        writer.write(body[split:])
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=3)
        writer.close()
        await writer.wait_closed()
        assert response.startswith(b"HTTP/1.1 401")
        assert b"must-not-run" not in response
    finally:
        await host.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
async def test_unexpected_leader_exit_reaps_children_and_runtime_dir(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    host = _host(
        tmp_path,
        environment={"KSADK_DSH_FAKE_CRASH_CHILD_PID_FILE": str(child_pid_file)},
    )
    runtime_dir: Path | None = None
    child_pid: int | None = None
    try:
        await host.start()
        runtime_dir = host._runtime_dir  # noqa: SLF001
        for _ in range(300):
            if child_pid_file.is_file():
                child_pid = int(child_pid_file.read_text())
            if child_pid is not None and host._runtime_dir is None:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)
        assert child_pid is not None
        assert host._runtime_dir is None  # noqa: SLF001
        assert runtime_dir is not None and not runtime_dir.exists()
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        await host.dispose()


@pytest.mark.asyncio
async def test_profile_projection_fence_opens_crash_breaker(tmp_path: Path) -> None:
    host = _host(tmp_path, config=b"different\n")
    host._explicit_environment["KSADK_DSH_FAKE_CONFIG"] = "[]\n"  # noqa: SLF001
    for _ in range(3):
        with pytest.raises(PluginHostError) as rejected:
            await host.start()
        assert rejected.value.code == "dsh_capability_profile_changed"
    with pytest.raises(PluginHostError) as opened:
        await host.start()
    assert opened.value.code == "dsh_capability_circuit_open"
    await host.dispose()


@pytest.mark.asyncio
async def test_unhealthy_generation_is_restarted_instead_of_reusing_dead_lease(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    try:
        first = await host.start()
        first_pid = host.pid
        real_probe = host._probe_health  # noqa: SLF001
        fail_once = True

        async def flapping_probe(lease, descriptor):  # noqa: ANN001, ANN202
            nonlocal fail_once
            if fail_once:
                fail_once = False
                return False
            return await real_probe(lease, descriptor)

        host._probe_health = flapping_probe  # type: ignore[method-assign]  # noqa: SLF001
        assert await host.health() is False

        second = await host.start()
        assert second.endpoint != first.endpoint
        assert host.pid is not None
        assert host.pid != first_pid
        assert await host.health() is True
    finally:
        await host.dispose()


@pytest.mark.asyncio
async def test_cancelled_start_cleans_process_and_preserves_cancellation(tmp_path: Path) -> None:
    host = _host(
        tmp_path,
        environment={"KSADK_DSH_FAKE_START_DELAY_MS": "10000"},
    )
    pending = asyncio.create_task(host.start())
    for _ in range(100):
        if host.pid is not None:
            break
        await asyncio.sleep(0.01)
    assert host.pid is not None

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert host.pid is None
    assert (await host.inventory()).state == "stopped"
    await host.dispose()


def test_profile_host_sets_fixed_node_heap_ceiling_and_denies_override(tmp_path: Path) -> None:
    host = _host(tmp_path)
    assert host._base_environment()["NODE_OPTIONS"] == "--max-old-space-size=512"  # noqa: SLF001

    node = shutil.which("node")
    assert node is not None
    with pytest.raises(PluginHostError) as denied:
        DshProfileCapabilityHost(
            (node, str(_fixture("dsh-capability-fake-profile.mjs"))),
            projection=_projection(),
            dsh_home=tmp_path / "other-home",
            environment={"NODE_OPTIONS": "--inspect"},
            node_command=node,
        )
    assert denied.value.code == "dsh_capability_environment_denied"


def test_ready_record_rejects_endpoint_or_inventory_tampering() -> None:
    root = Path(__file__).parents[2]
    fixture = json.loads(
        (root / "contracts/plugin/v1/fixtures/dsh-profile-capability-host.json").read_text()
    )["ready"]
    invalid_endpoint = {**fixture, "endpoint": "http://example.test/mcp"}
    with pytest.raises(ValueError):
        DshProfileCapabilityReady.model_validate(invalid_endpoint)
    invalid_inventory = {**fixture, "inventoryDigest": "sha256:" + "f" * 64}
    with pytest.raises(ValueError):
        DshProfileCapabilityReady.model_validate(invalid_inventory)
