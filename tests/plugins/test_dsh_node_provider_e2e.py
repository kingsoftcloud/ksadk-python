"""Real DSH install -> Cordis service -> Node provider host -> PluginHost E2E."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient

from ksadk.plugins.bridges.dsh import DshPluginMutationError, DshProfilePluginBridge
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.host import PluginExecutionContext, PluginHost, PluginHostError
from ksadk.plugins.providers.dsh import (
    DSH_HOST_USER_PERMISSION,
    DshAgentProviderFactory,
    DshAgentProviderHost,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import BundleManifest
from ksadk.studio.service import StudioService

PLUGIN_NAME = "@ksadk-test/dsh-node-agent-provider"
PROVIDER_REF = "plugin://io.ksadk.test.dsh-node-provider@1.0.0"

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 to install the pinned public npm toolchain",
)


def _fixture_bundle() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "dsh-node-agent-provider"


def _resolved_bundle(
    tmp_path: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
) -> ResolvedPluginBundle:
    composition = registry.resolve(profile)
    return ResolvedPluginBundle(
        root=tmp_path,
        manifest=BundleManifest(
            bundle_format="agentkit.bundle/v2",
            agent_id="dsh-cordis-node-agent",
            source_revision=1,
            resolved_digest="sha256:" + "1" * 64,
            runtime_type="dsh",
            plugin_lock_digest=composition.plugin_lock_digest,
            composition_profile_digest=composition.profile_digest,
            files=[],
            bundle_digest="sha256:" + "2" * 64,
        ),
        resolved_agent_spec=MappingProxyType(
            {
                "instructions": MappingProxyType(
                    {"system": "Keep the DSH Cordis activation alive across turns."}
                ),
                "execution": MappingProxyType({"strategy": "direct"}),
            }
        ),
        composition=composition,
    )


def _wait(client: TestClient, operation_id: str) -> dict:
    for _ in range(500):
        payload = client.get(f"/api/v1/operations/{operation_id}").json()
        if payload["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not finish")


def _studio_provider_spec() -> dict:
    return {
        "description": "Outside-repository DSH Node AgentProvider",
        "runtime": {"type": "plugin", "providerRef": PROVIDER_REF},
        "instructions": {"system": "Keep state across turns."},
        "model": {
            "provider": "openai-compatible",
            "model": "fixture-model",
            "endpointUrl": "https://model.example.test/v1/chat/completions",
            "credentialRef": "env://MODEL_API_KEY",
        },
        "security": {
            "allowedPermissions": ["process:host-user"],
            "network": {
                "mode": "restricted",
                "allowedHosts": ["model.example.test"],
                "allowPrivateNetwork": False,
            },
        },
    }


def test_normal_studio_discovers_runs_and_releases_external_node_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolchain = DshToolchainManager(base_dir=tmp_path / "toolchains")
    toolchain.install()
    dsh_home = tmp_path / "dsh-home"
    profile_name = "studio-node-e2e"
    with DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile=profile_name,
        dsh_command=toolchain.require_command(),
        cwd=toolchain.root,
    ) as bridge:
        installed = bridge.install_plugin(
            str(_fixture_bundle()),
            accept_host_permissions=True,
        )
        assert installed.installed is True
        assert installed.enabled is False
        assert PLUGIN_NAME not in bridge.project_profile().bundles
        enabled = bridge.set_enabled(PLUGIN_NAME, enabled=True)
        assert enabled.enabled is True

    monkeypatch.setenv("AGENTENGINE_PLUGIN_TOOLCHAIN_HOME", str(tmp_path / "toolchains"))
    monkeypatch.setenv("KSADK_DSH_HOME", str(dsh_home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", profile_name)
    monkeypatch.setenv("KSADK_DSH_BIN", toolchain.require_command()[0])

    workspace = tmp_path / "studio-workspace"
    studio = StudioService(workspace)
    app = create_studio_app(workspace, service=studio, security_enabled=False)
    with TestClient(app) as client:
        catalog = client.get("/api/v1/agent-providers")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["items"] == [
            {
                "providerRef": PROVIDER_REF,
                "pluginId": PLUGIN_NAME,
                "resolvedVersion": "1.0.0",
                "displayName": "DSH Cordis Node provider",
                "state": "enabled",
                "compatible": True,
                "selectable": True,
                "reason": None,
                "permissions": ["process:host-user"],
                "isolation": "sidecar",
                "configSchemaDeclared": False,
                "secretFields": [],
            }
        ]
        manager = studio._dsh_provider_registration_manager
        assert manager is not None
        assert manager.inventory.state == "bound"
        assert manager.inventory.packages[0].state == "bound"
        assert manager.host_pids
        plugin_inventory = client.get("/api/v1/plugin-ecosystems/dsh/plugins").json()
        assert plugin_inventory["items"][0]["runtimeState"] == {
            "state": "bound",
            "providerRef": PROVIDER_REF,
            "errorCode": None,
        }

        created = client.post(
            "/api/v1/agents",
            json={
                "id": "outside-node-provider",
                "name": "Outside Node Provider",
                "template": "blank",
                "spec": _studio_provider_spec(),
            },
        )
        assert created.status_code == 201, created.text
        build_operation = client.post(
            "/api/v1/agents/outside-node-provider/builds",
            headers={"Idempotency-Key": "outside-node-build"},
            json={"revision": 1},
        )
        build_done = _wait(client, build_operation.json()["id"])
        assert build_done["status"] == "SUCCEEDED", build_done
        build_id = build_done["resourceId"]

        first_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "outside-node-run-one"},
            json={"input": {"role": "user", "content": "first"}},
        )
        first_done = _wait(client, first_operation.json()["id"])
        assert first_done["status"] == "SUCCEEDED", first_done
        first = client.get(f"/api/v1/runs/{first_done['resourceId']}").json()
        assert first["status"] == "COMPLETED", first
        assert first["output"] == "first:turn-1"

        second_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "outside-node-run-two"},
            json={
                "sessionId": first["sessionId"],
                "input": {"role": "user", "content": "second"},
            },
        )
        second_done = _wait(client, second_operation.json()["id"])
        assert second_done["status"] == "SUCCEEDED", second_done
        second = client.get(f"/api/v1/runs/{second_done['resourceId']}").json()
        assert second["status"] == "COMPLETED", second
        assert second["sessionId"] == first["sessionId"]
        assert second["output"] == "second:turn-2"
        assert studio.plugin_runs.active_activation_count == 1

        disabled = client.post(
            f"/api/v1/plugin-ecosystems/dsh/plugins/{PLUGIN_NAME}:disable"
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["item"]["state"] == "disabled"
        assert disabled.json()["item"]["runtimeState"] == {
            "state": "installed",
            "providerRef": None,
            "errorCode": None,
        }
        assert client.get("/api/v1/agent-providers").json()["items"] == []
        assert studio.plugin_runs.active_activation_count == 0
        assert manager.host_pids == ()
        assert manager.inventory.packages[0].state == "installed"

        rejected_operation = client.post(
            f"/api/v1/builds/{build_id}/runs",
            headers={"Idempotency-Key": "outside-node-run-disabled"},
            json={"input": {"role": "user", "content": "must fail"}},
        )
        rejected = _wait(client, rejected_operation.json()["id"])
        assert rejected["status"] == "FAILED"

        removed = client.delete(
            f"/api/v1/plugin-ecosystems/dsh/plugins/{PLUGIN_NAME}"
        )
        assert removed.status_code == 204, removed.text
        assert manager.inventory.packages == ()
        assert client.get("/api/v1/plugin-ecosystems/dsh/plugins").json()["items"] == []


@pytest.mark.asyncio
async def test_real_dsh_bundle_drives_stateful_node_provider_and_uninstalls(
    tmp_path: Path,
) -> None:
    toolchain = DshToolchainManager(base_dir=tmp_path / "toolchains")
    toolchain.install()
    dsh_home = tmp_path / "dsh-home"
    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="ksadk-node-e2e",
        dsh_command=toolchain.require_command(),
        cwd=toolchain.root,
    )
    host: DshAgentProviderHost | None = None
    plugin_host: PluginHost | None = None
    session = None
    try:
        dsh_version = bridge.start().version
        assert dsh_version
        installed = bridge.install_plugin(
            str(_fixture_bundle()),
            accept_host_permissions=True,
        )
        assert installed.name == PLUGIN_NAME
        assert installed.enabled is False
        assert installed.version == "1.0.0"
        assert installed.source_kind == "directory"
        assert installed.source_digest is not None

        inactive_projection = bridge.project_profile()
        assert PLUGIN_NAME not in inactive_projection.bundles
        enabled = bridge.set_enabled(PLUGIN_NAME, enabled=True)
        assert enabled.enabled is True
        projection = bridge.project_profile()
        assert PLUGIN_NAME in projection.bundles
        profile_root = dsh_home / "profiles" / "ksadk-node-e2e"
        installed_host = (
            profile_root
            / "node_modules"
            / "@ksadk-test"
            / "dsh-node-agent-provider"
            / "provider-host.mjs"
        )
        assert installed_host.is_file()

        event_log = tmp_path / "cordis-events.log"
        host = DshAgentProviderHost(
            (os.environ.get("NODE", "node"), str(installed_host)),
            projection=projection,
            cwd=profile_root,
            environment={
                "KSADK_DSH_CORDIS_MODULE": str(
                    toolchain.resolve_module_entry("@deepseek-ai/cordis")
                ),
                "KSADK_DSH_EVENT_LOG": str(event_log),
            },
        )
        descriptor = await host.describe()
        assert descriptor.plugin_name == PLUGIN_NAME
        assert descriptor.profile_digest == projection.config_digest

        # The Cordis service, not the Python bridge, owns both immutable fences.
        with pytest.raises(PluginHostError) as profile_fence:
            await host._request(  # noqa: SLF001 - adversarial protocol E2E
                "preflight",
                {
                    "profileDigest": "sha256:" + "0" * 64,
                    "descriptorDigest": descriptor.descriptor_digest,
                },
            )
        assert profile_fence.value.code == "dsh_provider_remote_error"
        with pytest.raises(PluginHostError) as descriptor_fence:
            await host._request(  # noqa: SLF001 - adversarial protocol E2E
                "inventory",
                {"descriptorDigest": "sha256:" + "f" * 64},
            )
        assert descriptor_fence.value.code == "dsh_provider_remote_error"

        registration = await host.registration()
        registry = PluginRegistry([registration.manifest])
        profile = CompositionProfile.model_validate(
            {
                "agentProvider": {
                    "ref": (
                        f"plugin://{descriptor.provider_id}@{descriptor.provider_version}"
                    )
                }
            }
        )
        factory = DshAgentProviderFactory(host, registration)
        plugin_host = PluginHost(
            registry,
            {descriptor.provider_id: factory},
            allowed_permissions=frozenset({DSH_HOST_USER_PERMISSION}),
        )
        await plugin_host.apply(profile)
        bundle = _resolved_bundle(tmp_path, registry, profile)
        session = await plugin_host.open_activation(
            bundle,
            activation_key="persistent-session",
        )

        first = await session.execute({"message": "first turn"})
        second = await session.execute({"message": "second turn"})
        assert first["turn"] == 1
        assert second == {
            "provider": "dsh-cordis-node",
            "agentId": "dsh-cordis-node-agent",
            "turn": 2,
            "history": [
                {"message": "first turn"},
                {"message": "second turn"},
            ],
            "cancelled": False,
            "outputText": "second turn:turn-2",
        }
        assert (await host.inventory()).activation_count == 1

        assert factory.runtime is not None
        cancellable = await factory.runtime.prepare(
            bundle,
            capabilities=PluginExecutionContext(
                profile_digest=bundle.composition.profile_digest,
                plugin_lock_digest=bundle.composition.plugin_lock_digest,
                bindings=(),
            ),
        )
        await cancellable.start()
        await cancellable.cancel()
        cancelled = await cancellable.execute({"message": "after cancel"})
        assert cancelled["cancelled"] is True
        await cancellable.drain()
        await cancellable.dispose()

        await session.close()
        assert (await host.inventory()).activation_count == 0
        await plugin_host.dispose()
        plugin_host = None
        assert host.pid is None

        # Disable is a real Profile boundary. Once the admitted host is
        # disposed, the same installed Provider cannot create another host
        # while its Bundle is absent from the projected Profile.
        disabled = bridge.set_enabled(PLUGIN_NAME, enabled=False)
        assert disabled.enabled is False
        assert disabled.source_digest == installed.source_digest
        disabled_projection = bridge.project_profile()
        assert PLUGIN_NAME not in disabled_projection.bundles
        disabled_host = DshAgentProviderHost(
            (os.environ.get("NODE", "node"), str(installed_host)),
            projection=disabled_projection,
            cwd=profile_root,
            environment={
                "KSADK_DSH_CORDIS_MODULE": str(
                    toolchain.resolve_module_entry("@deepseek-ai/cordis")
                ),
                "KSADK_DSH_EVENT_LOG": str(event_log),
            },
        )
        with pytest.raises(PluginHostError) as disabled_provider:
            await disabled_host.registration()
        assert disabled_provider.value.code == "dsh_provider_remote_error"
        assert disabled_host.pid is None

        # DSH has no atomic local-source upgrade API. The bridge packs a new
        # immutable tgz and delegates add. This broken candidate changes npm
        # identity, so it cannot be the exact replacement requested by name;
        # manifest/lock/state/node_modules must all return to the old Provider.
        broken = tmp_path / "broken-node-provider-v2"
        shutil.copytree(_fixture_bundle(), broken)
        package_path = broken / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["version"] = "2.0.0"
        package["name"] = "@ksadk-test/dsh-node-agent-provider-broken"
        package_path.write_text(json.dumps(package), encoding="utf-8")
        before_manifest = (profile_root / "package.json").read_bytes()
        before_lock = (profile_root / "pnpm-lock.yaml").read_bytes()
        before_state = (profile_root / ".ksadk-dsh-plugins.json").read_bytes()
        with pytest.raises(DshPluginMutationError):
            bridge.update_plugin(
                PLUGIN_NAME,
                source=str(broken),
                accept_host_permissions=True,
            )
        assert (profile_root / "package.json").read_bytes() == before_manifest
        assert (profile_root / "pnpm-lock.yaml").read_bytes() == before_lock
        assert (profile_root / ".ksadk-dsh-plugins.json").read_bytes() == before_state
        rolled_back = bridge.get_plugin(PLUGIN_NAME)
        assert rolled_back.version == "1.0.0"
        assert rolled_back.enabled is False
        assert rolled_back.source_digest == installed.source_digest

        reenabled = bridge.set_enabled(PLUGIN_NAME, enabled=True)
        assert reenabled.enabled is True
        assert reenabled.version == "1.0.0"
        assert reenabled.source_digest == installed.source_digest
        restored_projection = bridge.project_profile()
        assert PLUGIN_NAME in restored_projection.bundles

        host = DshAgentProviderHost(
            (os.environ.get("NODE", "node"), str(installed_host)),
            projection=restored_projection,
            cwd=profile_root,
            environment={
                "KSADK_DSH_CORDIS_MODULE": str(
                    toolchain.resolve_module_entry("@deepseek-ai/cordis")
                ),
                "KSADK_DSH_EVENT_LOG": str(event_log),
            },
        )
        restored_registration = await host.registration()
        assert restored_registration.descriptor.provider_version == "1.0.0"
        restored_registry = PluginRegistry([restored_registration.manifest])
        restored_profile = CompositionProfile.model_validate(
            {
                "agentProvider": {
                    "ref": (
                        "plugin://"
                        f"{restored_registration.descriptor.provider_id}"
                        f"@{restored_registration.descriptor.provider_version}"
                    )
                }
            }
        )
        restored_factory = DshAgentProviderFactory(host, restored_registration)
        plugin_host = PluginHost(
            restored_registry,
            {
                restored_registration.descriptor.provider_id: restored_factory,
            },
            allowed_permissions=frozenset({DSH_HOST_USER_PERMISSION}),
        )
        await plugin_host.apply(restored_profile)
        restored_bundle = _resolved_bundle(tmp_path, restored_registry, restored_profile)
        session = await plugin_host.open_activation(
            restored_bundle,
            activation_key="persistent-session",
        )
        after_rollback = await session.execute({"message": "after rollback"})
        assert after_rollback == {
            "provider": "dsh-cordis-node",
            "agentId": "dsh-cordis-node-agent",
            # Provider process state is intentionally not durable across a
            # disable/dispose boundary. A new admitted activation starts at 1.
            "turn": 1,
            "history": [{"message": "after rollback"}],
            "cancelled": False,
            "outputText": "after rollback:turn-1",
        }
        await session.close()
        await plugin_host.dispose()
        plugin_host = None
        assert host.pid is None

        bridge.uninstall_plugin(PLUGIN_NAME)
        assert bridge.list_plugins() == ()
        removed_projection = bridge.project_profile()
        assert PLUGIN_NAME not in removed_projection.bundles
        with pytest.raises(PluginHostError) as unavailable:
            await session.execute({"message": "must not run after uninstall"})
        assert unavailable.value.code == "agent_activation_closed"

        # pnpm may retain content-addressed bytes, but the current DSH Profile is
        # authoritative: even a stale executable cannot re-enter the selector.
        removed_host = DshAgentProviderHost(
            (os.environ.get("NODE", "node"), str(installed_host)),
            projection=removed_projection,
            cwd=profile_root,
            environment={
                "KSADK_DSH_CORDIS_MODULE": str(
                    toolchain.resolve_module_entry("@deepseek-ai/cordis")
                ),
                "KSADK_DSH_EVENT_LOG": str(event_log),
            },
        )
        with pytest.raises(PluginHostError) as removed_bundle:
            await removed_host.describe()
        assert removed_bundle.value.code in {
            # pnpm may remove the executable completely...
            "dsh_provider_protocol_invalid",
            # ...or retain store-backed bytes that reject the inactive Profile.
            "dsh_provider_remote_error",
        }
        assert removed_host.pid is None

        events = event_log.read_text(encoding="utf-8").splitlines()
        assert "cordis:effect:active" in events
        assert "cordis:execute:cordis-node-1:1" in events
        assert "cordis:execute:cordis-node-1:2" in events
        assert "cordis:cancel:cordis-node-2" in events
        assert events[-1] == "cordis:effect:disposed"
    finally:
        if session is not None and not session.closed:
            await session.close()
        if plugin_host is not None:
            await plugin_host.dispose()
        elif host is not None and host.pid is not None:
            await host.dispose()
        bridge.close()
