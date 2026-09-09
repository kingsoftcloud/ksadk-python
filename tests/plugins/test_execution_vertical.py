"""Real Bundle -> resolver -> PluginHost -> provider execution vertical."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ksadk.plugins.bundle import (
    PluginBundleError,
    PluginBundleResolver,
    ResolvedPluginBundle,
)
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHost, PluginHostError
from ksadk.plugins.resolver import PluginRegistry
from ksadk.studio.contracts import BundleManifest, FileEntry


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    mode: str = "unique",
    digit: str,
) -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": plugin_id, "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.plugins.test_execution_vertical:factory",
                "provides": [
                    {"definition": definition, "slot": slot, "mode": mode}
                ],
                "isolation": "in-process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": "sha256:" + digit * 64,
                },
            },
        }
    )


def _catalog() -> tuple[PluginRegistry, CompositionProfile]:
    manifests = [
        _manifest(
            "io.ksadk.provider",
            definition="agent.provider/v1",
            slot="agent.execution",
            digit="1",
        ),
        _manifest(
            "io.ksadk.mcp",
            definition="mcp.connector/v1",
            slot="capability.mcp.fixture",
            mode="multiple",
            digit="3",
        ),
        _manifest(
            "io.ksadk.skill",
            definition="skill.source/v1",
            slot="capability.skill.fixture",
            mode="multiple",
            digit="4",
        ),
        _manifest(
            "io.ksadk.context",
            definition="context.contributor/v1",
            slot="context.fixture",
            mode="multiple",
            digit="5",
        ),
    ]
    profile = CompositionProfile.model_validate(
        {
            "agentProvider": {"ref": "plugin://io.ksadk.provider@1.0.0"},
            "capabilities": [
                {"ref": f"plugin://{manifest.metadata.id}@1.0.0"}
                for manifest in manifests[1:]
            ],
        }
    )
    return PluginRegistry(manifests), profile


def _write_bundle(
    root: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
) -> ResolvedPluginBundle:
    root.mkdir()
    composition = registry.resolve(profile)
    payloads = {
        "composition-profile.json": profile.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "plugin-lock.json": composition.plugin_lock.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "resolved-agent-spec.json": {
            "name": "vertical-fixture",
            "instructions": {"system": "Use the composed capabilities."},
        },
    }
    files: list[FileEntry] = []
    for relative, payload in payloads.items():
        content = _json_bytes(payload)
        (root / relative).write_bytes(content)
        files.append(FileEntry(path=relative, sha256=_sha256(content), size=len(content)))

    manifest = BundleManifest(
        bundle_format="agentkit.bundle/v2",
        agent_id="vertical-fixture",
        source_revision=1,
        resolved_digest="sha256:" + "a" * 64,
        plugin_lock_digest=composition.plugin_lock_digest,
        composition_profile_digest=composition.profile_digest,
        files=files,
    )
    manifest_payload = manifest.model_dump(
        by_alias=True,
        exclude={"bundle_digest"},
        exclude_none=True,
        mode="json",
    )
    manifest.bundle_digest = _sha256(_json_bytes(manifest_payload))
    (root / "manifest.json").write_bytes(
        _json_bytes(manifest.model_dump(by_alias=True, exclude_none=True, mode="json"))
    )
    return PluginBundleResolver(registry).resolve(root)


@dataclass
class _CapabilityRuntime:
    plugin_id: str
    calls: list[str]

    async def start(self) -> None:
        self.calls.append(f"plugin.start:{self.plugin_id}")

    async def health(self) -> bool:
        self.calls.append(f"plugin.health:{self.plugin_id}")
        return True

    async def drain(self) -> None:
        self.calls.append(f"plugin.drain:{self.plugin_id}")

    async def dispose(self) -> None:
        self.calls.append(f"plugin.dispose:{self.plugin_id}")

    async def prepare_value(self, bundle: ResolvedPluginBundle) -> str:
        self.calls.append(f"capability.prepare:{self.plugin_id}")
        return f"{self.plugin_id}:{bundle.manifest.agent_id}"

@dataclass
class _Activation:
    provider: "_ProviderRuntime"
    prepared: list[str]
    calls: list[str]
    fail_start: bool = False

    async def start(self) -> None:
        self.calls.append("activation.start")
        if self.fail_start:
            raise RuntimeError("activation bootstrap failed")

    async def health(self) -> bool:
        self.calls.append("activation.health")
        return True

    async def execute(self, request: Any) -> Any:
        self.calls.append("activation.execute")
        return await self.provider.run(request, self.prepared)

    async def drain(self) -> None:
        self.calls.append("activation.drain")

    async def dispose(self) -> None:
        self.calls.append("activation.dispose")


class _ProviderRuntime(_CapabilityRuntime):
    def __init__(self, plugin_id: str, calls: list[str]) -> None:
        super().__init__(plugin_id=plugin_id, calls=calls)
        self.fail_activation_start = False
        self.fail_execution = False

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> _Activation:
        self.calls.append("provider.prepare")
        prepared: list[str] = []
        for definition in (
            "mcp.connector/v1",
            "skill.source/v1",
            "context.contributor/v1",
        ):
            runtime = capabilities.require(definition).runtime
            assert isinstance(runtime, _CapabilityRuntime)
            prepared.append(await runtime.prepare_value(bundle))
        return _Activation(
            provider=self,
            prepared=prepared,
            calls=self.calls,
            fail_start=self.fail_activation_start,
        )

    async def run(self, request: Any, prepared: list[str]) -> dict[str, Any]:
        self.calls.append("provider.execute")
        if self.fail_execution:
            raise RuntimeError("provider turn failed")
        return {"request": request, "prepared": prepared}


class _Factory:
    def __init__(self, plugin_id: str, calls: list[str]) -> None:
        self.plugin_id = plugin_id
        self.calls = calls
        self.provider: _ProviderRuntime | None = None

    async def stage(self, manifest, *, profile, services):  # noqa: ANN001, ARG002
        self.calls.append(f"plugin.stage:{manifest.metadata.id}")
        if self.plugin_id == "io.ksadk.provider":
            self.provider = _ProviderRuntime(self.plugin_id, self.calls)
            return self.provider
        return _CapabilityRuntime(self.plugin_id, self.calls)


def _host(registry: PluginRegistry) -> tuple[PluginHost, list[str], _Factory]:
    calls: list[str] = []
    ids = (
        "io.ksadk.provider",
        "io.ksadk.mcp",
        "io.ksadk.skill",
        "io.ksadk.context",
    )
    factories = {plugin_id: _Factory(plugin_id, calls) for plugin_id in ids}
    return PluginHost(registry, factories), calls, factories["io.ksadk.provider"]


@pytest.mark.asyncio
async def test_bundle_resolver_and_host_execute_composed_provider(tmp_path: Path) -> None:
    registry, profile = _catalog()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    host, calls, _provider_factory = _host(registry)

    inventory = await host.apply(profile)
    result = await host.execute(bundle, {"text": "hello"})

    assert inventory.profile_digest == bundle.composition.profile_digest
    assert {entry.id for entry in bundle.composition.plugin_lock.plugins} == {
        "io.ksadk.provider",
        "io.ksadk.mcp",
        "io.ksadk.skill",
        "io.ksadk.context",
    }
    assert result == {
        "request": {"text": "hello"},
        "prepared": [
            "io.ksadk.mcp:vertical-fixture",
            "io.ksadk.skill:vertical-fixture",
            "io.ksadk.context:vertical-fixture",
        ],
    }
    assert [
        call
        for call in calls
        if call.startswith(("provider.", "capability.", "activation."))
    ] == [
        "provider.prepare",
        "capability.prepare:io.ksadk.mcp",
        "capability.prepare:io.ksadk.skill",
        "capability.prepare:io.ksadk.context",
        "activation.start",
        "activation.health",
        "activation.execute",
        "provider.execute",
        "activation.drain",
        "activation.dispose",
    ]

    with pytest.raises(TypeError):
        bundle.resolved_agent_spec["instructions"]["system"] = "mutated"


def test_bundle_resolver_rejects_undeclared_plugin_input(tmp_path: Path) -> None:
    registry, profile = _catalog()
    bundle_root = tmp_path / "agent-bundle"
    _write_bundle(bundle_root, registry, profile)
    (bundle_root / "runtime-hook.py").write_text("raise SystemExit\n", encoding="utf-8")

    with pytest.raises(PluginBundleError, match="outside its integrity manifest") as captured:
        PluginBundleResolver(registry).resolve(bundle_root)

    assert captured.value.code == "plugin_bundle_file_undeclared"


@pytest.mark.asyncio
async def test_activation_initialization_failure_is_cleaned_and_graph_remains_ready(
    tmp_path: Path,
) -> None:
    registry, profile = _catalog()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    host, calls, provider_factory = _host(registry)
    stable = await host.apply(profile)
    assert provider_factory.provider is not None
    provider_factory.provider.fail_activation_start = True

    with pytest.raises(PluginHostError, match="activation bootstrap failed") as captured:
        await host.execute(bundle, {"text": "first"})

    assert captured.value.code == "agent_activation_start_failed"
    assert calls[-3:] == [
        "activation.start",
        "activation.drain",
        "activation.dispose",
    ]
    assert host.inventory() == stable

    provider_factory.provider.fail_activation_start = False
    recovered = await host.execute(bundle, {"text": "second"})
    assert recovered["request"] == {"text": "second"}


@pytest.mark.asyncio
async def test_activation_session_reuses_provider_owned_state_across_turns_and_closes(
    tmp_path: Path,
) -> None:
    registry, profile = _catalog()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    host, calls, _provider_factory = _host(registry)
    await host.apply(profile)

    first_handle = await host.open_activation(bundle, activation_key="session-1")
    first = await first_handle.execute({"text": "first"})
    second_handle = await host.open_activation(bundle, activation_key="session-1")
    second = await second_handle.execute({"text": "second"})

    assert first["request"] == {"text": "first"}
    assert second["request"] == {"text": "second"}
    assert first_handle.key == second_handle.key == "session-1"
    assert first_handle.bundle_digest == second_handle.bundle_digest
    assert calls.count("provider.prepare") == 1
    assert calls.count("activation.start") == 1
    assert calls.count("activation.execute") == 2
    assert host.activation_count == 1

    await second_handle.close()

    assert first_handle.closed is True
    assert host.activation_count == 0
    assert calls.count("activation.drain") == 1
    assert calls.count("activation.dispose") == 1
    await host.dispose()


@pytest.mark.asyncio
async def test_activation_failure_is_fail_closed_and_next_turn_prepares_fresh_state(
    tmp_path: Path,
) -> None:
    registry, profile = _catalog()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    host, calls, provider_factory = _host(registry)
    await host.apply(profile)
    assert provider_factory.provider is not None

    failed_handle = await host.open_activation(bundle, activation_key="session-1")
    provider_factory.provider.fail_execution = True
    with pytest.raises(PluginHostError, match="provider turn failed") as captured:
        await failed_handle.execute({"text": "fail"})

    assert captured.value.code == "agent_execution_failed"
    assert failed_handle.closed is True
    assert host.activation_count == 0
    assert calls.count("activation.drain") == 1
    assert calls.count("activation.dispose") == 1

    provider_factory.provider.fail_execution = False
    recovered_handle = await host.open_activation(bundle, activation_key="session-1")
    recovered = await recovered_handle.execute({"text": "recover"})

    assert recovered["request"] == {"text": "recover"}
    assert calls.count("provider.prepare") == 2
    assert host.activation_count == 1
    await host.dispose()
    assert recovered_handle.closed is True
    assert host.activation_count == 0


@pytest.mark.asyncio
async def test_profile_update_keeps_existing_session_pinned_until_it_closes(
    tmp_path: Path,
) -> None:
    registry, profile = _catalog()
    bundle = _write_bundle(tmp_path / "agent-bundle", registry, profile)
    host, calls, _provider_factory = _host(registry)
    await host.apply(profile)
    handle = await host.open_activation(bundle, activation_key="session-1")

    replacement = CompositionProfile.model_validate(
        {
            "agentProvider": {"ref": "plugin://io.ksadk.provider@1.0.0"},
            "capabilities": [
                {"ref": "plugin://io.ksadk.mcp@1.0.0"},
                {"ref": "plugin://io.ksadk.skill@1.0.0"},
            ],
        }
    )
    await host.apply(replacement)

    # A profile switch changes default admission for *new* sessions only. The
    # existing provider-native session keeps its old graph so it cannot lose a
    # Codex thread / DSH checkpoint half-way through a conversation.
    assert handle.closed is False
    pinned = await host.open_activation(bundle, activation_key="session-1")
    assert pinned.bundle_digest == handle.bundle_digest
    assert await pinned.execute({"text": "continue old session"}) == {
        "request": {"text": "continue old session"},
        "prepared": [
            "io.ksadk.mcp:vertical-fixture",
            "io.ksadk.skill:vertical-fixture",
            "io.ksadk.context:vertical-fixture",
        ],
    }
    assert host.activation_count == 1
    assert "activation.drain" not in calls

    await pinned.close()

    # Only once the last pin is released may the retired graph drain. The new
    # profile itself remains the host's default until final disposal.
    assert host.activation_count == 0
    activation_drain = calls.index("activation.drain")
    old_provider_drain = calls.index("plugin.drain:io.ksadk.provider")
    assert activation_drain < old_provider_drain
    await host.dispose()
