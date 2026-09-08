"""Formal Studio Codex Build admission and native Provider execution."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import yaml

from ksadk.plugins.providers.codex import CodexAgentProviderFactory
from ksadk.runtime import StartRequest
from ksadk.studio.codex_builder import CodexBuildRecord, CodexStudioBuilder
from ksadk.studio.codex_provider_build import CODEX_PROVIDER_REF
from ksadk.studio.contracts import AgentSpec, Instructions, ModelSpec, RuntimeRef
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from tests.plugins.test_codex_provider_vertical import (
    _provider_manifest,
    _StrictCodexBackend,
    _StrictCodexClient,
)


def _studio(tmp_path):
    backend = _StrictCodexBackend()
    backend.thread_configs = []

    class RecordingClient(_StrictCodexClient):
        async def start_thread(self, config=None):
            backend.thread_configs.append(dict(config or {}))
            return await super().start_thread(config)

    def client(config=None):
        backend.configs.append(config)
        return RecordingClient(backend)

    manifest = _provider_manifest()
    manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(update={"isolation": "sidecar"}),
        }
    )
    studio = StudioService(
        tmp_path,
        credential_resolver=SimpleNamespace(resolve=lambda ref: "fixture-not-a-secret"),
        codex_runtime_inspector=lambda runtime: ("0.8.4", "0.147.0", "codex-cli 0.147.0"),
        plugin_provider_manifests={CODEX_PROVIDER_REF: manifest},
        plugin_provider_factories={
            CODEX_PROVIDER_REF: CodexAgentProviderFactory(
                codex_client_factory=client,
            )
        },
    )
    studio.codex_agents.create(
        agent_id="codex-local",
        spec=AgentSpec(
            runtime=RuntimeRef(type="codex", version="0.147.0"),
            model=ModelSpec(
                model="fixture-codex-model",
                credential_ref="env://FIXTURE_KEY",
                base_url="https://fixture.invalid/v1",
            ),
            instructions=Instructions(system="Answer the user."),
        ),
    )
    studio._started = True  # Registry fixture substitutes for managed DSH startup.
    return studio, backend


@pytest.mark.asyncio
async def test_formal_build_uses_provider_and_preserves_native_launch(tmp_path):
    studio, backend = _studio(tmp_path)
    try:
        record = studio.codex_builder.build("codex-local")
        assert record.local_execution == "provider"
        assert record.provider_bundle.provider_ref == CODEX_PROVIDER_REF
        spec = studio.codex_runs.resolve(record.id, sandbox="read_only")
        assert spec.plugin_bundle_root is not None
        assert spec.launch_context.project_dir == tmp_path
        adapter = studio._scheduler_adapter_provider(spec)()
        handle = await adapter.start(
            StartRequest(
                input="Hello",
                user_id="fixture",
                session_id="session-provider",
                agent_id=spec.agent_id,
                model=spec.model,
                config=spec.request_config,
            )
        )
        events = [event async for event in adapter.stream(handle)]
        assert events
        assert backend.turn_count == 1
        assert backend.turn_configs[-1]["sandbox"] == "read-only"
        assert backend.thread_configs[-1]["cwd"] == str(tmp_path)
        assert record.id in str(backend.configs[-1].env["CODEX_HOME"])
        await adapter.close(handle)
        artifact = yaml.safe_load(studio.workspace.resolve(record.artifact_path).read_text())
        assert artifact["runtime"]["name"] == "codex"
        assert "providerBundle" not in artifact
        assert "localExecution" not in artifact
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_disabled_provider_refuses_build_and_saved_run_without_fallback(tmp_path):
    studio, backend = _studio(tmp_path)
    try:
        record = studio.codex_builder.build("codex-local")
        prepared_spec = studio.codex_runs.resolve(record.id)
        studio._active_provider_manifests = {}
        assert not studio.codex_builder.is_current(record)
        for action in (
            lambda: studio.codex_builder.build("codex-local"),
            lambda: studio.codex_runs.resolve(record.id),
        ):
            with pytest.raises(StudioError) as error:
                action()
            assert error.value.code == "AGENT_PROVIDER_NOT_REGISTERED"
        await studio.plugin_runs.suspend_admission()
        studio.plugin_runs.replace_provider_registrations({}, {})
        await studio.plugin_runs.resume_admission()
        failed = await studio.run_service.run(prepared_spec, "Cached admission", session_id="stale")
        assert failed.status.value == "FAILED"
        assert backend.thread_count == 0
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_legacy_record_remains_explicit_direct_compatibility(tmp_path):
    studio, backend = _studio(tmp_path)
    try:
        studio.codex_builder.provider_build = None
        legacy = studio.codex_builder.build("codex-local")
        payload = legacy.model_dump(by_alias=True, mode="json")
        payload.pop("localExecution")
        payload.pop("providerBundle")
        restored = CodexBuildRecord.model_validate(payload)
        assert restored.local_execution == "legacy"
        studio.codex_builds.save(restored)
        studio._active_provider_manifests = {}
        spec = studio.codex_runs.resolve(restored.id)
        assert spec.plugin_bundle_root is None
        assert CodexStudioBuilder._build_id("a" * 64, {}) == "build_" + "a" * 20
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_run_build_keeps_full_request_and_repeat_build_reference(tmp_path, monkeypatch):
    studio, backend = _studio(tmp_path)
    try:
        record = studio.codex_builder.build("codex-local")
        repeated = studio.codex_builder.build("codex-local")
        assert record.id == repeated.id

        def forbidden_global_kernel(spec):
            raise AssertionError("Provider Build cannot reuse a process-global direct Kernel")

        monkeypatch.setattr(studio.run_service, "_kernel_runtime_for_spec", forbidden_global_kernel)
        result = await studio.run_build(
            record.id,
            "Hello",
            "formal-run",
            sandbox="read_only",
            runtime_input=[
                {"type": "text", "text": "Hello"},
                {"type": "mention", "name": "fixture-file", "path": "/fixture/file"},
            ],
            reasoning_effort="high",
        )
        assert result.status.value == "COMPLETED", result.error
        assert backend.turn_count == 1
        assert backend.turn_configs[-1]["sandbox"] == "read-only"
        assert backend.turn_configs[-1]["effort"] == "high"
        assert "fixture-file" in str(backend.prompts[-1])
        assert backend.closed == 1
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KSADK_CODEX_PROVIDER_E2E") != "1", reason="opt-in real App Server")
async def test_formal_studio_build_runs_real_app_server(tmp_path, monkeypatch):
    import dataclasses

    from tests.e2e.codex_app_server_fixture import RealCodexFactory
    from tests.e2e.codex_responses_stub import DeterministicResponsesStub

    monkeypatch.setenv("KSADK_CODEX_USE_PROXY", "0")
    studio, _ = _studio(tmp_path)
    with DeterministicResponsesStub() as responses:
        factory = RealCodexFactory(responses_url=responses.base_url)

        def local_client(config):
            env = dict(config.env)
            env.update(
                {
                    "OPENAI_BASE_URL": responses.base_url,
                    "OPENAI_API_BASE": responses.base_url,
                    "KSADK_CODEX_USE_PROXY": "0",
                    "OPENAI_API_KEY": "fixture-not-a-secret",
                }
            )
            return factory(dataclasses.replace(config, env=env))

        studio.plugin_runs._provider_factories[CODEX_PROVIDER_REF] = CodexAgentProviderFactory(
            codex_client_factory=local_client,
        )
        try:
            record = studio.codex_builder.build("codex-local")
            result = await studio.run_build(record.id, "Hello from Studio", "real-studio")
            assert result.status.value == "COMPLETED", result.error
            assert "bridge skill received" in str(studio.event_store.events(result.id))
            assert responses.requests()[0].payload["model"] == "fixture-codex-model"
        finally:
            await studio.aclose()
    assert factory.processes
    assert all(process.poll() is not None for process in factory.processes)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "cancel"])
async def test_formal_run_keeps_live_approval_and_cancel(tmp_path, decision):
    import asyncio

    from tests.runners.test_adapter_contract import _FakeCodexClient

    class ApprovalClient(_FakeCodexClient):
        def __init__(self):
            super().__init__()
            self.decisions = []

        async def resolve_approval(self, approval_id, value):
            self.decisions.append((approval_id, value))
            self._release.set()
            return True

    client = ApprovalClient()
    studio, _ = _studio(tmp_path)
    studio.plugin_runs._provider_factories[CODEX_PROVIDER_REF] = CodexAgentProviderFactory(
        codex_client_factory=lambda config=None: client,
    )
    events = []
    pending = asyncio.Event()

    def observe(event):
        events.append(event)
        if event.type == "a2ui.interaction":
            pending.set()

    task = None
    try:
        build = studio.codex_builder.build("codex-local")
        task = asyncio.create_task(
            studio.run_build(build.id, "Ask approval", "control", on_event=observe)
        )
        await asyncio.wait_for(pending.wait(), timeout=5)
        interaction = next(event for event in events if event.type == "a2ui.interaction")
        if decision == "cancel":
            await studio.run_service.cancel_run(interaction.run_id)
        else:
            await studio.run_service.submit_interaction(
                interaction.run_id,
                interaction.data["interactionId"],
                name="approve",
                expected_revision=1,
                idempotency_key="fixture-approval",
            )
        if decision == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert studio.event_store.get(interaction.run_id).status == "CANCELLED"
            assert client.interrupted
        else:
            result = await asyncio.wait_for(task, timeout=5)
            assert result.status.value == "COMPLETED", result.error
            assert client.decisions
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await studio.aclose()


@pytest.mark.asyncio
async def test_native_plugin_lock_reaches_provider_factory(tmp_path):
    from ksadk.studio.contracts import NativePluginBinding
    from tests.studio.test_codex_plugin_store import _observed, _plugin_root

    studio, _ = _studio(tmp_path)
    try:
        snapshot = studio.codex_plugin_snapshots.commit(
            _observed(_plugin_root(tmp_path / "source"))
        )
        draft = studio.codex_agents._project(studio.codex_manifests.load("codex-local"))
        draft.spec.bindings.plugins = [
            NativePluginBinding(
                ecosystem="codex",
                plugin_ref=snapshot.plugin_ref,
                snapshot_digest=snapshot.snapshot_digest,
                components=["skill:alpha"],
            )
        ]
        studio.codex_agents.update(
            "codex-local", draft.spec, expected_revision=draft.metadata.revision
        )
        build = studio.codex_builder.build("codex-local")
        spec = studio.codex_runs.resolve(build.id)
        native = await studio.plugin_runs.kernel_adapter(spec, session_id="plugins")
        from ksadk.codex.client import CodexPluginBootstrap

        assert native._plugin_bootstrap == CodexPluginBootstrap.from_mapping(
            spec.launch_context.config["codex_plugin_bootstrap"],
        )
        assert native._plugin_bootstrap.plugin_names == ("fixture-plugin",)
        assert build.provider_bundle is not None
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("different_connection", [False, True])
async def test_model_override_is_locked_and_connection_change_is_explicit(
    tmp_path, different_connection
):
    studio, backend = _studio(tmp_path)
    try:
        profiles = []
        for index, model in enumerate(("fixture-codex-model", "fixture-second-model")):
            address = (
                "https://second.invalid/v1"
                if different_connection and index
                else "https://fixture.invalid/v1"
            )
            profiles.append(
                studio.catalog.create_model_profile(
                    name=f"fixture-{index}",
                    display_name=f"Fixture {index}",
                    version="1.0.0",
                    description="Local test profile",
                    spec=ModelSpec(
                        model=model,
                        base_url=address,
                        credential_ref="env://FIXTURE_KEY",
                    ),
                )
            )
        draft = studio.codex_agents._project(studio.codex_manifests.load("codex-local"))
        draft.spec.bindings.model_profile_id = profiles[0].resource_id
        draft.spec.bindings.model_profile_ids = [profile.resource_id for profile in profiles]
        studio.codex_agents.update(
            "codex-local", draft.spec, expected_revision=draft.metadata.revision
        )
        if different_connection:
            with pytest.raises(StudioError) as error:
                studio.codex_builder.build("codex-local")
            assert error.value.code == "CODEX_PROVIDER_MODEL_CONNECTION_UNSUPPORTED"
        else:
            build = studio.codex_builder.build("codex-local")
            result = await studio.run_build(
                build.id, "Hello", "model-override", model="fixture-second-model"
            )
            assert result.status.value == "COMPLETED", result.error
            assert backend.turn_configs[-1]["model"] == "fixture-second-model"
            assert backend.configs[-1].env["OPENAI_BASE_URL"] == "https://fixture.invalid/v1"
    finally:
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rename", [False, True])
async def test_provider_keeps_frozen_skill_bytes_after_catalog_drift(tmp_path, rename):
    from pathlib import Path

    from ksadk.studio.contracts import CapabilityBinding

    studio, backend = _studio(tmp_path)
    try:
        source = tmp_path / "capabilities/skills/guide"
        source.mkdir(parents=True)
        (source / "skill.yaml").write_text("version: 1.0.0\n")
        frozen = "---\nname: guide\ndescription: fixture\n---\nOriginal frozen instructions.\n"
        (source / "SKILL.md").write_text(frozen)
        draft = studio.codex_agents._project(studio.codex_manifests.load("codex-local"))
        draft.spec.bindings.skills = [CapabilityBinding(resource_id="skill:local:guide:1.0.0")]
        studio.codex_agents.update(
            "codex-local", draft.spec, expected_revision=draft.metadata.revision
        )
        build = studio.codex_builder.build("codex-local")
        (source / "SKILL.md").write_text("Changed mutable instructions.")
        if rename:
            source.rename(source.with_name("changed"))
        result = await studio.run_build(build.id, "Use guide", "skill-drift")
        assert result.status.value == "COMPLETED", result.error
        skill = next(item for item in backend.prompts[-1][1] if type(item).__name__ == "SkillInput")
        assert skill.name == "guide"
        assert Path(skill.path).read_text() == frozen
        assert build.id in skill.path
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_yaml_only_model_inventory_survives_provider_build(tmp_path):
    studio, backend = _studio(tmp_path)
    try:
        snapshot = studio.codex_manifests.load("codex-local")
        studio.codex_manifests.save(
            snapshot.manifest.model_copy(
                update={
                    "models": ["fixture-codex-model", "fixture-yaml-alternative"],
                }
            )
        )
        build = studio.codex_builder.build("codex-local")
        result = await studio.run_build(
            build.id,
            "Hello",
            "yaml-model",
            model="fixture-yaml-alternative",
        )
        assert result.status.value == "COMPLETED", result.error
        assert backend.turn_configs[-1]["model"] == "fixture-yaml-alternative"
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_local_provider_keeps_existing_native_credential_policy(tmp_path):
    studio, backend = _studio(tmp_path)
    references = []

    def missing_optional_key(reference):
        references.append(reference)
        raise StudioError("SECRET_NOT_FOUND", "Fixture has no API key", status_code=404)

    studio.credentials.resolve = missing_optional_key
    try:
        build = studio.codex_builder.build("codex-local")
        result = await studio.run_build(build.id, "Native authentication", "native-auth")
        assert result.status.value == "COMPLETED", result.error
        assert backend.turn_count == 1
        # Only the original Codex resolver chooses optional environment/native
        # authentication; the generic Bundle must not demand the draft key.
        assert references and set(references) == {"env://OPENAI_API_KEY"}
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_registration_change_rejects_old_build_and_runs_rebuilt(tmp_path):
    studio, backend = _studio(tmp_path)
    try:
        first = studio.codex_builder.build("codex-local")
        old_manifest = studio._active_provider_manifests[CODEX_PROVIDER_REF]
        changed = old_manifest.model_copy(
            update={
                "spec": old_manifest.spec.model_copy(
                    update={"health_contract": "plugin.health/v2"}
                ),
            }
        )
        studio._active_provider_manifests = {CODEX_PROVIDER_REF: changed}
        studio.plugin_compositions.replace_provider_registrations(studio._active_provider_manifests)
        studio.plugin_runs.replace_provider_registrations(
            studio._active_provider_manifests,
            studio.plugin_runs._provider_factories,
        )
        second = studio.codex_builder.build("codex-local")
        assert first.provider_bundle.bundle_digest != second.provider_bundle.bundle_digest
        assert first.id != second.id
        with pytest.raises(StudioError) as error:
            studio.codex_runs.resolve(first.id)
        assert error.value.code == "CODEX_PROVIDER_CHANGED"
        result = await studio.run_build(second.id, "New registration", "registration-change")
        assert result.status.value == "COMPLETED", result.error
        assert backend.turn_count == 1
    finally:
        await studio.aclose()
