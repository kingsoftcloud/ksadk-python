"""Standard DSH Profile -> fixed host -> PluginHost AgentProvider vertical."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import MappingProxyType

import pytest

from ksadk.plugins.bridges import dsh as dsh_bridge
from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile
from ksadk.plugins.host import PluginExecutionContext, PluginHost, PluginHostError
from ksadk.plugins.providers.dsh import (
    DSH_HOST_USER_PERMISSION,
    DshAgentProviderDescriptor,
    DshAgentProviderFactory,
    DshAgentProviderHost,
    DshAgentProviderPreflight,
    DshCircuitBreaker,
    dsh_agent_provider_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.studio.contracts import BundleManifest

PLUGIN_NAME = "@example/dsh-agent-provider"

_HOST_SOURCE = r"""
import json
import os
import sys

PROTOCOL = "ksadk.dsh-agent-provider-host/v1"
METHODS = sorted({
    "handshake", "describe", "preflight", "activate", "inventory",
    "execute", "cancel", "health", "drain", "dispose",
})
EVENT_LOG = os.environ.get("DSH_EVENT_LOG", "")
profile = None
descriptor_digest = None
activations = {}
next_id = 0

def record(value):
    if EVENT_LOG:
        with open(EVENT_LOG, "a", encoding="utf-8") as stream:
            stream.write(value + "\n")

def respond(request_id, result):
    sys.stdout.write(json.dumps({"id": request_id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    method = request["method"]
    params = request.get("params") or {}
    record(method)
    if method == "handshake":
        profile = params["profile"]
        respond(request_id, {
            "protocolVersion": PROTOCOL,
            "methods": METHODS,
            "hostVersion": "0.1.2-alpha.1",
        })
    elif method == "describe":
        profile = params["profile"]
        respond(request_id, {
            "descriptorFormat": "dsh.agent-provider-descriptor/v1",
            "ecosystem": "dsh",
            "providerId": "io.example.dsh-provider",
            "providerVersion": "1.2.3",
            "displayName": "Example DSH provider",
            "pluginName": "@example/dsh-agent-provider",
            "profile": profile["profile"],
            "profileDigest": profile["configDigest"],
            "definition": "agent.provider/v1",
            "slot": "agent.execution",
            "runtimeProtocols": ["agentkit.runtime/v1"],
        })
    elif method == "preflight":
        descriptor_digest = params["descriptorDigest"]
        respond(request_id, {
            "ready": True,
            "descriptorDigest": descriptor_digest,
            "profileDigest": params["profileDigest"],
        })
    elif method == "activate":
        next_id += 1
        activation_id = f"dsh-activation-{next_id}"
        activations[activation_id] = params["bundle"]["manifest"]["agentId"]
        respond(request_id, {"activationId": activation_id})
    elif method == "inventory":
        respond(request_id, {
            "providerId": "io.example.dsh-provider",
            "providerVersion": "1.2.3",
            "profile": profile["profile"],
            "profileDigest": profile["configDigest"],
            "descriptorDigest": params["descriptorDigest"],
            "state": "ready",
            "activationCount": len(activations),
        })
    elif method == "health":
        activation_id = params.get("activationId")
        respond(request_id, {"healthy": not activation_id or activation_id in activations})
    elif method == "execute":
        activation_id = params["activationId"]
        respond(request_id, {
            "provider": "dsh",
            "agentId": activations[activation_id],
            "echo": params["request"],
        })
    elif method == "cancel":
        record("cancel:" + params["activationId"])
        respond(request_id, {"ok": True})
    elif method == "drain":
        respond(request_id, {"ok": True})
    elif method == "dispose":
        activation_id = params.get("activationId")
        if activation_id:
            activations.pop(activation_id, None)
        respond(request_id, {"ok": True})
"""


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _standard_dsh_profile(home: Path) -> None:
    root = home / "profiles" / "studio"
    _write_json(
        root / "package.json",
        {
            "dependencies": {PLUGIN_NAME: "1.2.3"},
            "dsh": {
                "profile": {
                    "bundles": ["@deepseek-ai/dsh-base", PLUGIN_NAME],
                }
            },
        },
    )
    package = root / "node_modules" / "@example" / "dsh-agent-provider"
    _write_json(
        package / "package.json",
        {
            "name": PLUGIN_NAME,
            "displayName": "Example DSH provider",
            "version": "1.2.3",
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
        },
    )
    (package / "cordis.patch.yml").write_text(
        "plugins:\n  - id: example-agent-provider\n", encoding="utf-8"
    )


class _DshCommandRunner:
    def __call__(self, command, _cwd, environment):  # noqa: ANN001
        assert environment["DSH_HOME"]
        if command[-1] == "--version":
            return dsh_bridge._CommandResult(stdout="dsh 0.1.2-alpha.1\n")
        if command[-1] == "--dump-config":
            return dsh_bridge._CommandResult(stdout="plugins:\n  - id: fixture\n")
        return dsh_bridge._CommandResult()


def _host_command(tmp_path: Path) -> tuple[str, str]:
    script = tmp_path / "fixed_dsh_provider_host.py"
    script.write_text(textwrap.dedent(_HOST_SOURCE), encoding="utf-8")
    return sys.executable, str(script)


def _projection() -> dsh_bridge.DshProfileProjection:
    return dsh_bridge.DshProfileProjection(
        profile="studio",
        bundles=(PLUGIN_NAME,),
        config_digest="sha256:" + "6" * 64,
        config_bytes=64,
        host_version="0.1.2-alpha.1",
    )


def _bundle(
    tmp_path: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
) -> ResolvedPluginBundle:
    composition = registry.resolve(profile)
    return ResolvedPluginBundle(
        root=tmp_path,
        manifest=BundleManifest(
            bundle_format="agentkit.bundle/v2",
            agent_id="dsh-agent",
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
                "instructions": MappingProxyType({"system": "Use the DSH profile."}),
                "execution": MappingProxyType({"strategy": "direct"}),
            }
        ),
        composition=composition,
    )


@pytest.mark.asyncio
async def test_standard_dsh_profile_exposes_provider_through_fixed_host(
    tmp_path: Path,
) -> None:
    dsh_home = tmp_path / "dsh-home"
    _standard_dsh_profile(dsh_home)
    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="studio",
        dsh_command=("dsh-fixture",),
        command_runner=_DshCommandRunner(),
    )
    bridge.start()
    projection = bridge.project_profile()
    event_log = tmp_path / "host-events.log"
    dsh_host = DshAgentProviderHost(
        _host_command(tmp_path),
        projection=projection,
        environment={"DSH_EVENT_LOG": str(event_log)},
    )

    descriptor = await dsh_host.describe()
    assert descriptor.plugin_name == PLUGIN_NAME
    assert descriptor.profile_digest == projection.config_digest
    registration = await dsh_host.registration()
    assert registration.preflight.ready is True

    manifest = registration.manifest
    registry = PluginRegistry([manifest])
    profile = CompositionProfile.model_validate(
        {
            "agentProvider": {
                "ref": f"plugin://{descriptor.provider_id}@{descriptor.provider_version}"
            }
        }
    )
    factory = DshAgentProviderFactory(dsh_host, registration)
    plugin_host = PluginHost(
        registry,
        {descriptor.provider_id: factory},
        allowed_permissions=frozenset({DSH_HOST_USER_PERMISSION}),
    )
    inventory = await plugin_host.apply(profile)
    assert [item.id for item in inventory.plugins] == [descriptor.provider_id]

    bundle = _bundle(tmp_path, registry, profile)
    session = await plugin_host.open_activation(bundle, activation_key="session-1")
    result = await session.execute({"message": "hello from KsADK"})
    assert result == {
        "provider": "dsh",
        "agentId": "dsh-agent",
        "echo": {"message": "hello from KsADK"},
    }
    assert (await dsh_host.inventory()).activation_count == 1
    await session.close()
    assert (await dsh_host.inventory()).activation_count == 0

    assert factory.runtime is not None
    direct = await factory.runtime.prepare(
        bundle,
        capabilities=PluginExecutionContext(
            profile_digest=bundle.composition.profile_digest,
            plugin_lock_digest=bundle.composition.plugin_lock_digest,
            bindings=(),
        ),
    )
    await direct.start()
    await direct.cancel()
    await direct.drain()
    await direct.dispose()
    assert (await dsh_host.inventory()).activation_count == 0

    await plugin_host.dispose()
    assert dsh_host.pid is None
    events = event_log.read_text(encoding="utf-8").splitlines()
    assert {
        "handshake",
        "describe",
        "preflight",
        "activate",
        "inventory",
        "cancel",
        "cancel:dsh-activation-2",
    } <= set(events)
    assert events[-1] == "dispose"


@pytest.mark.asyncio
async def test_descriptor_for_inactive_dsh_bundle_is_rejected_before_activation(
    tmp_path: Path,
) -> None:
    projection = dsh_bridge.DshProfileProjection(
        profile="studio",
        bundles=("@deepseek-ai/dsh-base",),
        config_digest="sha256:" + "3" * 64,
        config_bytes=64,
        host_version="0.1.2-alpha.1",
    )
    dsh_host = DshAgentProviderHost(_host_command(tmp_path), projection=projection)

    with pytest.raises(PluginHostError) as raised:
        await dsh_host.describe()
    assert raised.value.code == "dsh_provider_descriptor_mismatch"
    await dsh_host.dispose()


def test_dsh_circuit_allows_one_recovery_probe() -> None:
    now = [10.0]
    circuit = DshCircuitBreaker(
        failure_threshold=2,
        recovery_timeout=5.0,
        clock=lambda: now[0],
    )

    assert circuit.acquire() is True
    circuit.fail()
    assert circuit.acquire() is True
    circuit.fail()
    assert circuit.snapshot().state == "open"
    assert circuit.acquire() is False

    now[0] += 5.0
    assert circuit.acquire() is True
    assert circuit.acquire() is False
    circuit.succeed()
    assert circuit.snapshot().state == "closed"


@pytest.mark.asyncio
async def test_dsh_host_opens_circuit_after_transport_failure(tmp_path: Path) -> None:
    broken = tmp_path / "broken_dsh_host.py"
    broken.write_text("import sys\nsys.stdin.readline()\n", encoding="utf-8")
    host = DshAgentProviderHost(
        (sys.executable, str(broken)),
        projection=_projection(),
        circuit_failure_threshold=1,
        circuit_recovery_timeout=60,
    )

    with pytest.raises(PluginHostError) as first:
        await host.describe()
    assert first.value.code == "dsh_provider_protocol_invalid"
    assert host.circuit_snapshot.state == "open"

    with pytest.raises(PluginHostError) as second:
        await host.describe()
    assert second.value.code == "dsh_provider_circuit_open"
    await host.dispose()


def test_dsh_host_rejects_secret_environment_values(tmp_path: Path) -> None:
    projection = dsh_bridge.DshProfileProjection(
        profile="studio",
        bundles=(PLUGIN_NAME,),
        config_digest="sha256:" + "4" * 64,
        config_bytes=64,
        host_version="0.1.2-alpha.1",
    )
    with pytest.raises(PluginHostError) as raised:
        DshAgentProviderHost(
            _host_command(tmp_path),
            projection=projection,
            environment={"DSH_API_TOKEN": "must-not-cross"},
        )
    assert raised.value.code == "dsh_provider_environment_denied"


def test_provider_cannot_enter_selector_before_matching_preflight() -> None:
    descriptor = DshAgentProviderDescriptor(
        provider_id="io.example.dsh-provider",
        provider_version="1.2.3",
        display_name="Example DSH provider",
        plugin_name=PLUGIN_NAME,
        profile="studio",
        profile_digest="sha256:" + "5" * 64,
    )
    not_ready = DshAgentProviderPreflight(
        ready=False,
        descriptor_digest=descriptor.descriptor_digest,
        profile_digest=descriptor.profile_digest,
    )

    with pytest.raises(PluginHostError) as raised:
        dsh_agent_provider_manifest(descriptor, preflight=not_ready)
    assert raised.value.code == "dsh_provider_not_ready"
