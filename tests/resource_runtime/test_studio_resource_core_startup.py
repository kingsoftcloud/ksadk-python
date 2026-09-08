"""Real pinned DSH CLI startup through Studio, with private resource IPC overlay."""

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_toolchain import DSH_VERSION, DshToolchainManager
from ksadk.resource_runtime.langgraph import create_bound_resource_tools
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from tests.resource_runtime.test_worker_process import initialization
from tests.resource_runtime.test_worker_process import upstream as upstream


@pytest.mark.skipif(
    not os.environ.get("KSADK_TEST_RESOURCE_CLI_ROOT"),
    reason="Requires pinned CLI installation at KSADK_TEST_RESOURCE_CLI_ROOT",
)
async def test_studio_starts_real_core_before_admitting_resource_worker(
    tmp_path, monkeypatch, upstream,
):
    root = Path(os.environ["KSADK_TEST_RESOURCE_CLI_ROOT"]).resolve()
    pnpm = root / "pnpm/node_modules/.bin/pnpm"
    dsh = root / "toolchains/dsh" / DSH_VERSION / "node_modules/.bin/dsh"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(pnpm.parent) + os.pathsep + os.environ["PATH"])
    dsh_home = tmp_path / "dsh-home"
    bundles = Path(__file__).resolve().parents[2] / "ksadk/plugins/providers/bundles"

    def install():
        with DshProfilePluginBridge(
            dsh_home=dsh_home, profile="resource-startup", dsh_command=(str(dsh),), cwd=tmp_path,
        ) as bridge:
            for name in (
                "dsh-platform-resources", "dsh-knowledge", "dsh-memory", "dsh-skill-center",
            ):
                installed = bridge.install_plugin(str(bundles / name), accept_host_permissions=True)
                bridge.set_enabled(installed.name, enabled=True)
            return bridge.snapshot_for_build()

    expected = await asyncio.to_thread(install)
    settings = dsh_home / "profiles/resource-startup/pnpm-workspace.yaml"
    assert yaml.safe_load(settings.read_text())["nodeLinker"] == "isolated"
    service = StudioDshCapabilityService(
        tmp_path, dsh_home=dsh_home, profile="resource-startup", dsh_command=(str(dsh),),
    )
    endpoint, calls = upstream
    try:
        try:
            descriptor, generation = await service.prepare_resource_generation(expected)
        except Exception:
            host = service._host
            if host is not None:
                pytest.fail("Core startup failed: " + "\n".join(host.stderr_tail))
            raise
        supervisor = service._resource_supervisor
        assert supervisor is not None and not supervisor._running
        assert not supervisor._registry._leases  # Core startup is not user/resource admission.
        socket_path = supervisor._socket.path
        assert socket_path.is_socket()
        host = service._host
        overlay = host._runtime_dir / "cordis.patch.yml"
        assert str(socket_path) in overlay.read_text()
        assert str(socket_path) not in expected.model_dump_json()
        payload = initialization(endpoint).pipe_payload()
        payload["resourceSnapshot"]["dshProfile"] = expected.model_dump(by_alias=True)
        snapshot = ResourceSnapshot.model_validate(payload["resourceSnapshot"])
        payload["scopes"][0].update(
            generationId=generation, profileDigest=descriptor.profile_digest,
            bindingSnapshotDigest=snapshot.digest,
        )
        active = await service.activate_resources(
            WorkerInitialization.model_validate(payload), expected=expected,
        )
        assert active.socket_path == socket_path
        assert service._resource_supervisor is supervisor
        async with create_bound_resource_tools(
            service._lease, active.leases,
            tool_aliases={"kb": "search_knowledge_base"},
        ) as tools:
            # All three business bundles are installed in this actual Core;
            # only the admitted knowledge binding is exposed to this run.
            assert [tool.name for tool in tools] == ["kb"]
            result = await tools[0].ainvoke({
                "type": "tool_call", "name": "kb", "id": "startup-call",
                "args": {"query": "real-core-startup"},
            })
            assert result.status == "success"
            assert "worker-result" in result.content
        assert len(calls) == 1 and calls[0][1]["DatasetId"] == "kb-selected"
        await service.aclose()
        assert host.pid is None
        assert not overlay.exists() and not socket_path.exists()
        assert not supervisor._running
    finally:
        await service.aclose()


def test_explicit_toolchain_does_not_require_default_managed_directory(tmp_path):
    executable = tmp_path / "dsh"
    executable.write_text(f"#!/bin/sh\nprintf '{DSH_VERSION}\\n'\n")
    executable.chmod(0o700)
    manager = DshToolchainManager(base_dir=tmp_path / "absent-managed")
    assert manager.require_command(executable) == (str(executable),)
    assert not manager.root.exists()
