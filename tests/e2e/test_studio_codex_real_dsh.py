"""Opt-in official DSH installation/registration -> Studio Build -> real Codex.

Requires the pinned CLI toolchain used by the resource Core E2E. DSH may install
public npm dependencies; all model traffic uses the local no-auth Responses stub.
The real Python descriptor host registers the official Provider, while the real
Node Core owns the installed/enabled Profile and capability generation.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge, dsh_subprocess_environment
from ksadk.plugins.dsh_toolchain import DSH_VERSION
from ksadk.plugins.providers.codex_dsh import (
    SHIPPED_CODEX_DSH_PACKAGE,
    KsADKCodexDshBridgeFactory,
)
from ksadk.studio.codex_provider_build import CODEX_PROVIDER_REF
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    Instructions,
    ModelSpec,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from ksadk.studio.dsh_provider_registration import StudioDshProviderRegistrationManager
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from tests.e2e.codex_app_server_fixture import RealCodexFactory
from tests.e2e.codex_responses_stub import DeterministicResponsesStub


@pytest.mark.skipif(
    not os.environ.get("KSADK_TEST_RESOURCE_CLI_ROOT")
    or os.environ.get("KSADK_CODEX_PROVIDER_E2E") != "1",
    reason="Requires pinned KSADK_TEST_RESOURCE_CLI_ROOT and KSADK_CODEX_PROVIDER_E2E=1",
)
async def test_real_core_registration_build_run_and_disable(tmp_path, monkeypatch):
    root = Path(os.environ["KSADK_TEST_RESOURCE_CLI_ROOT"]).resolve()
    pnpm = root / "pnpm/node_modules/.bin/pnpm"
    dsh = root / "toolchains/dsh" / DSH_VERSION / "node_modules/.bin/dsh"
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dsh_home = tmp_path / "dsh-home"
    for key, value in {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "KSADK_CODEX_HOME": str(tmp_path / "codex-home"),
        "KSADK_DSH_HOME": str(dsh_home),
        "KSADK_DSH_PROFILE": "web",
        "KSADK_DSH_BIN": str(dsh),
        "PATH": str(pnpm.parent) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(repo),
        "KSADK_CODEX_USE_PROXY": "0",
    }.items():
        monkeypatch.setenv(key, value)

    def install():
        subprocess.run(
            [str(dsh), "--profile", "web", "--dump-config"],
            cwd=workspace,
            env=dsh_subprocess_environment(dsh_home=dsh_home),
            check=True, capture_output=True, timeout=30,
        )
        with DshProfilePluginBridge(
            dsh_home=dsh_home, profile="web", dsh_command=(str(dsh),), cwd=workspace,
        ) as bridge:
            installed = bridge.install_plugin(
                str(repo / "ksadk/plugins/providers/bundles/ksadk-codex"),
                accept_host_permissions=True,
            )
            assert installed.name == SHIPPED_CODEX_DSH_PACKAGE
            bridge.set_enabled(installed.name, enabled=True)
            bridge.migrate_to_isolated_layout(accept_host_permissions=True)
            return bridge.snapshot_for_build()

    expected = await asyncio.to_thread(install)
    manager = StudioDshProviderRegistrationManager(
        workspace, dsh_home=dsh_home, profile="web", dsh_command=(str(dsh),),
    )
    capabilities = StudioDshCapabilityService(
        workspace, dsh_home=dsh_home, profile="web", dsh_command=(str(dsh),),
    )

    class FixtureCredentials:
        def resolve(self, ref):
            assert ref == "env://FIXTURE_KEY"
            return "fixture-not-a-secret"

    studio = StudioService(
        workspace, credential_resolver=FixtureCredentials(),
        dsh_provider_registration_manager=manager, dsh_capability_service=capabilities,
    )
    with DeterministicResponsesStub() as responses:
        # Only the native client constructor is configured for the local stub;
        # no Provider factory, manifest, registry or DSH host is replaced.
        factory = RealCodexFactory(responses_url=responses.base_url)
        for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
            monkeypatch.setenv(name, responses.base_url)
        monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-secret")

        def local_client(config):
            env = dict(config.env)
            env.update(
                OPENAI_BASE_URL=responses.base_url,
                OPENAI_API_BASE=responses.base_url,
                OPENAI_API_KEY="fixture-not-a-secret",
                KSADK_CODEX_USE_PROXY="0",
            )
            return factory(dataclasses.replace(config, env=env))

        monkeypatch.setattr("ksadk.plugins.providers.codex_native.AsyncCodexClient", local_client)
        try:
            snapshot = await capabilities.capability_snapshot()
            core_host = capabilities._host
            assert snapshot.inventory.state == "ready"
            assert core_host.pid is not None
            assert snapshot.descriptor.profile_digest == expected.projection.config_digest
            await studio.start()
            assert manager.host_pids
            assert CODEX_PROVIDER_REF in studio._active_provider_manifests
            assert isinstance(
                studio.plugin_runs._provider_factories[CODEX_PROVIDER_REF],
                KsADKCodexDshBridgeFactory,
            )
            model = ModelSpec(
                model="fixture-codex-model", base_url=responses.base_url,
                wire_api="responses", credential_ref="env://FIXTURE_KEY",
            )
            profile = studio.catalog.create_model_profile(
                name="fixture-model", display_name="Local fixture", version="1.0.0",
                description="Local no-auth Responses stub", spec=model,
            )
            studio.codex_agents.create(
                agent_id="codex-real-core",
                spec=AgentSpec(
                    runtime=RuntimeRef(type="codex", version="0.147.0"), model=model,
                    instructions=Instructions(system="Answer the user."),
                    bindings=AgentBindings(
                        model_profile_id=profile.resource_id,
                        model_profile_ids=[profile.resource_id],
                    ),
                ),
            )
            # Profile install consent is separate from per-Agent permission.
            with pytest.raises(StudioError) as error:
                studio.codex_builder.build("codex-real-core")
            assert error.value.code == "AGENT_PROVIDER_PERMISSION_DENIED"
            draft = studio.codex_agents._project(studio.codex_manifests.load("codex-real-core"))
            draft.spec.security = SecuritySpec(allowed_permissions=["process:host-user"])
            studio.codex_agents.update(
                "codex-real-core", draft.spec, expected_revision=draft.metadata.revision,
            )
            build = studio.codex_builder.build("codex-real-core")
            assert build.local_execution == "provider"
            assert build.provider_bundle.provider_ref == CODEX_PROVIDER_REF
            result = await studio.run_build(
                build.id, "Hello from real Core", "real-core-run", sandbox="read_only",
            )
            assert result.status.value == "COMPLETED", result.error
            assert "bridge skill received" in str(studio.event_store.events(result.id))
            assert len(responses.requests()) == 1
            assert responses.requests()[0].payload["model"] == model.model

            def disable():
                with DshProfilePluginBridge(
                    dsh_home=dsh_home, profile="web", dsh_command=(str(dsh),), cwd=workspace,
                ) as bridge:
                    bridge.set_enabled(SHIPPED_CODEX_DSH_PACKAGE, enabled=False)

            async def mutate():
                await asyncio.to_thread(disable)

            await studio.reconfigure_dsh_profile(mutate)
            assert CODEX_PROVIDER_REF not in studio._active_provider_manifests
            assert not manager.host_pids
            assert core_host.pid is None
            with pytest.raises(StudioError) as error:
                await studio.run_build(build.id, "Must reject", "disabled-run")
            assert error.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
            with pytest.raises(StudioError) as error:
                studio.codex_builder.build("codex-real-core")
            assert error.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
            assert len(responses.requests()) == 1
        finally:
            await studio.aclose()
    assert factory.processes
    assert all(process.poll() is not None for process in factory.processes)
    assert not manager.host_pids
