"""Normal Studio startup -> managed DSH Harness registration vertical."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.bridges.dsh import DshPluginInventory, DshProfileProjection
from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
from ksadk.plugins.providers.legacy_catalog import legacy_harness_agent_provider_manifest
from ksadk.studio.contracts import (
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RunStatus,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.dsh_provider_registration import (
    StudioDshProviderInventory,
    StudioDshProviderRegistrationError,
    StudioDshProviderRegistrationManager,
    StudioDshProviderRegistrations,
    merge_provider_registrations,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.plugins.dsh_home import default_studio_dsh_home, prepare_studio_dsh_home


class _Reasoner:
    def __init__(self) -> None:
        self.turns: list[list[dict[str, object]]] = []

    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        del prompt, tools
        assert model == "fixture-model"  # Provider resolves its locked profile before invocation.
        snapshot = [dict(item) for item in messages]
        self.turns.append(snapshot)
        prior = [item.get("content") for item in snapshot if item.get("role") == "assistant"]
        return HarnessReasoningTurn(
            final_text="second:remembered" if "first:stored" in prior else "first:stored"
        )


def _managed_profile(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "dsh-home"
    prepare_studio_dsh_home(home)
    profile = home / "profiles" / "studio"
    installed = profile / "node_modules" / "@kingsoftcloud" / "ksadk-harness-provider"
    installed.parent.mkdir(parents=True)
    shutil.copytree(shipped_harness_dsh_bundle().root, installed)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@kingsoftcloud/ksadk-harness-provider": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@kingsoftcloud/ksadk-harness-provider"]}},
            }
        ),
        encoding="utf-8",
    )
    executable = tmp_path / "dsh-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *--dump-config*) echo 'profile: studio; harness: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", "studio")
    monkeypatch.setenv("KSADK_DSH_BIN", str(executable))
    return installed


def _spec() -> AgentSpec:
    return AgentSpec(
        runtime=RuntimeRef(type="harness"),
        instructions=Instructions(system="Retain the canonical conversation."),
        model=ModelSpec(
            model="fixture-model",
            endpoint_url="https://model.example.test/v1/chat/completions",
            credential_ref="env://MODEL_API_KEY",
        ),
        security=SecuritySpec(
            allowed_permissions=["process:host-user"],
            network=NetworkPolicy(allowed_hosts=["model.example.test"]),
        ),
    )


@pytest.mark.asyncio
async def test_normal_studio_discovers_binds_and_runs_managed_harness_two_turns(
    tmp_path: Path, monkeypatch
) -> None:
    _managed_profile(tmp_path, monkeypatch)
    reasoner = _Reasoner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = StudioDshProviderRegistrationManager.discover(workspace)
    assert manager is not None
    registrations = await manager.start()
    assert registrations.inventory.state == "ready"
    assert registrations.inventory.packages[0].state == "ready"
    studio = StudioService(
        workspace,
        harness_reasoner=reasoner,
        dsh_provider_registration_manager=manager,
    )
    studio.create_agent(
        agent_id="managed-harness",
        name="Managed Harness",
        spec=_spec(),
    )

    build = await studio.ensure_current_build("managed-harness")
    assert manager.inventory.state == "bound"
    assert manager.inventory.providers == ("plugin://io.ksadk.harness-provider@1.0.0",)
    # Registration discovery is bounded; the consuming PluginHost owns the
    # independently fenced execution sidecar.
    assert manager.host_pid is None

    first = await studio.run_build(build.id, "first", "managed-session")
    second = await studio.run_build(build.id, "second", "managed-session")

    assert first.status == RunStatus.COMPLETED, first.error
    assert first.output == "first:stored"
    assert second.status == RunStatus.COMPLETED, second.error
    assert second.output == "second:remembered"
    assert studio.plugin_runs.active_activation_count == 1

    await studio.aclose()
    assert manager.inventory.state == "disposed"
    assert manager.host_pid is None
    assert studio.plugin_runs.active_activation_count == 0


@pytest.mark.asyncio
async def test_tampered_managed_bundle_fails_closed_without_builtin_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    installed = _managed_profile(tmp_path, monkeypatch)
    (installed / "index.mjs").write_text("export default 'tampered'\n", encoding="utf-8")
    studio = StudioService(tmp_path / "workspace", harness_reasoner=_Reasoner())
    studio.create_agent(agent_id="must-fail", name="Must fail", spec=_spec())

    with pytest.raises(StudioError) as captured:
        await studio.ensure_current_build("must-fail")
    assert captured.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
    assert studio.builds.list_for_agent("must-fail") == []
    manager = studio._dsh_provider_registration_manager
    assert manager is not None
    assert manager.inventory.state == "bound"
    assert manager.inventory.providers == ()
    assert manager.inventory.packages[0].state == "failed"
    assert manager.inventory.packages[0].error_code == "harness_dsh_bundle_digest_mismatch"

    await studio.aclose()


@pytest.mark.asyncio
async def test_client_only_dsh_profile_does_not_block_studio_startup(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "dsh-home"
    prepare_studio_dsh_home(home)
    profile = home / "profiles" / "studio"
    profile.mkdir(parents=True)
    installed = profile / "node_modules" / "@example" / "studio-client"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text(
        json.dumps(
            {
                "name": "@example/studio-client",
                "version": "1.0.0",
                "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
            }
        ),
        encoding="utf-8",
    )
    (installed / "cordis.patch.yml").write_text("client: true\n", encoding="utf-8")
    (profile / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@example/studio-client": "1.0.0"},
                "dsh": {"profile": {"bundles": ["@example/studio-client"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KSADK_DSH_HOME", str(home))
    monkeypatch.setenv("KSADK_DSH_PROFILE", "studio")
    executable = tmp_path / "dsh-client-fixture"
    executable.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *--version*) echo 0.1.1-rc.2;;\n"
        "  *--dump-config*) echo 'profile: studio; client: 1.0.0';;\n"
        "  *) exit 2;;\n"
        "esac\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("KSADK_DSH_BIN", str(executable))

    studio = StudioService(tmp_path / "workspace")
    await studio.start()

    manager = studio._dsh_provider_registration_manager
    assert manager is not None
    assert manager.inventory.state == "bound"
    assert manager.inventory.providers == ()
    assert manager.inventory.packages[0].package_name == "@example/studio-client"
    assert manager.inventory.packages[0].state == "enabled"
    await studio.aclose()


def test_multiple_inconsistent_registration_sources_are_rejected() -> None:
    inventory = StudioDshProviderInventory(state="ready", profile="studio")
    manifest = legacy_harness_agent_provider_manifest()
    provider_ref = "plugin://io.ksadk.harness-provider@1.0.0"
    first = StudioDshProviderRegistrations(
        manifests={provider_ref: manifest},
        factories={provider_ref: object()},
        inventory=inventory,
    )
    second = StudioDshProviderRegistrations(
        manifests={provider_ref: manifest},
        factories={provider_ref: object()},
        inventory=inventory,
    )

    with pytest.raises(StudioDshProviderRegistrationError) as captured:
        merge_provider_registrations(first, second)
    assert captured.value.code == "dsh_provider_registration_conflict"


def test_official_default_marker_is_scoped_to_the_owned_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manager = StudioDshProviderRegistrationManager(
        workspace,
        dsh_home=workspace / ".agentkit" / "dsh-home",
        profile="web",
        dsh_command=("dsh",),
    )
    legacy_marker = workspace / ".agentkit" / "official-dsh-defaults.json"
    legacy_marker.parent.mkdir(parents=True)
    legacy_marker.write_text(
        json.dumps({"version": 1, "codexProviderApplied": True}),
        encoding="utf-8",
    )

    assert manager._default_marker_path == (  # noqa: SLF001 - migration contract
        workspace / ".agentkit" / "dsh-home" / "official-dsh-defaults-web.json"
    )
    assert manager._read_default_marker(manager._default_marker_path) == {}  # noqa: SLF001


def test_owned_default_profile_repairs_legacy_hoisted_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_DSH_HOME", raising=False)
    monkeypatch.delenv("KSADK_DSH_PROFILE", raising=False)
    workspace = tmp_path / "workspace"
    home = default_studio_dsh_home(workspace)
    prepare_studio_dsh_home(home)
    profile = home / "profiles/web"
    profile.mkdir(parents=True)
    (profile / "pnpm-workspace.yaml").write_text("nodeLinker: hoisted\n")
    calls = []

    class Bridge:
        def migrate_to_isolated_layout(self, **kwargs) -> None:  # noqa: ANN003
            calls.append(kwargs)

    manager = StudioDshProviderRegistrationManager(
        workspace,
        dsh_home=home,
        profile="web",
        dsh_command=("dsh",),
    )

    manager._repair_owned_profile_layout(Bridge())  # type: ignore[arg-type]  # noqa: SLF001

    assert calls == [{
        "accept_host_permissions": True,
        "recover_external_dependency_links": True,
    }]


def test_resource_profile_adds_official_transport_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KSADK_DSH_HOME", raising=False)
    monkeypatch.delenv("KSADK_DSH_PROFILE", raising=False)
    workspace = tmp_path / "workspace"
    profile = workspace / ".agentkit/dsh-home/profiles/agentkit-resources"
    profile.mkdir(parents=True)
    manifest = profile / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "dsh-profile-agentkit-resources",
                "private": True,
                "dependencies": {},
                "dsh": {
                    "profile": {
                        "bundles": [
                            "@deepseek-ai/dsh-base",
                            "@kingsoftcloud/dsh-platform-resources",
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = StudioDshProviderRegistrationManager(
        workspace,
        dsh_home=workspace / ".agentkit/dsh-home",
        profile="agentkit-resources",
        dsh_command=("dsh",),
    )

    manager._ensure_resource_runtime_bundle()  # noqa: SLF001
    manager._ensure_resource_runtime_bundle()  # noqa: SLF001 - idempotence

    bundles = json.loads(manifest.read_text(encoding="utf-8"))["dsh"]["profile"][
        "bundles"
    ]
    assert bundles == [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-web-app",
        "@kingsoftcloud/dsh-platform-resources",
    ]


class _OfficialCoreProfileBridge:
    def __init__(self, **_kwargs) -> None:  # noqa: ANN003
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:  # noqa: ANN002
        return None

    def list_plugins(self) -> tuple[DshPluginInventory, ...]:
        return (
            DshPluginInventory(
                profile="web",
                name="@example/community-plugin",
                display_name="Community Plugin",
                version="1.0.0",
                requested_spec="@example/community-plugin@1.0.0",
                enabled=True,
            ),
        )

    def project_profile(self) -> DshProfileProjection:
        return DshProfileProjection(
            profile="web",
            bundles=(
                "@deepseek-ai/dsh-base",
                "@deepseek-ai/dsh-web-app",
                "@example/community-plugin",
            ),
            config_digest="sha256:" + "c" * 64,
            config_bytes=128,
            host_version="0.1.2-rc.1",
        )


def test_official_core_bundles_do_not_conflict_with_plugin_inventory(tmp_path: Path) -> None:
    manager = StudioDshProviderRegistrationManager(
        tmp_path,
        dsh_home=tmp_path / ".agentkit" / "dsh-home",
        profile="web",
        dsh_command=("dsh",),
        bridge_factory=_OfficialCoreProfileBridge,
    )

    snapshot = manager._discover_profile()  # noqa: SLF001 - profile contract

    assert snapshot.projection.bundles[:2] == (
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-web-app",
    )
    assert snapshot.packages[0].name == "@example/community-plugin"
