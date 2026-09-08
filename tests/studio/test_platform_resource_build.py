from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile
from ksadk.plugins.providers.dsh_capabilities import DshMcpConnectorLease
from ksadk.plugins.providers.legacy_catalog import legacy_harness_agent_provider_manifest
from ksadk.plugins.providers.platform_resources import (
    PLATFORM_RESOURCE_MCP_REF,
    PlatformResourceMCPRuntime,
    platform_resource_mcp_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.resource_runtime.build_artifacts import (
    ResourceBuildReference,
    restore_resource_build,
)
from ksadk.resource_runtime.leases import ResourceLease
from ksadk.resource_runtime.supervisor import ActiveResources
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    BundleManifest,
    Instructions,
    MemorySpec,
    MemoryWriteSpec,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_authority import (
    AdmittedResourceAccess,
    VerifiedResourceAuthority,
)
from ksadk.studio.resource_connections import ResourceConnectionDeclaration
from ksadk.studio.service import StudioService
from tests.resource_runtime.test_studio_resource_config import binding


class _Authority:
    def __init__(self, connections) -> None:
        self.connections = connections
        self.calls: list[tuple[str, int | None]] = []

    def admit(self, config, *, expected_connection_revision=None):
        self.calls.append((config.binding.id, expected_connection_revision))
        now = datetime.now(timezone.utc)
        operations = (
            ("load_memory", "save_memory", "memory_status")
            if config.binding.resource.kind == "memory-instance"
            else ("search_knowledge_base",)
        )
        return VerifiedResourceAuthority(
            connection_ref="connection-a",
            connection_revision=expected_connection_revision,
            tenant_ref="tenant-a",
            resource_principal_ref="principal-a",
            resource=config.binding.resource,
            allowed_operations=operations,
            issuer_endpoint="https://iam.example.test",
            issuer_region="region-a",
            data_endpoint="https://knowledge.example.test",
            observed_at=now,
            expires_at=now + timedelta(minutes=1),
            request_id="fixture-request",
        )

    def admit_runtime(self, config, *, expected_connection_revision=None):
        grant = self.admit(
            config,
            expected_connection_revision=expected_connection_revision,
        )
        return AdmittedResourceAccess(
            authority=grant,
            credentials=self.connections.resolve_credentials(
                self.connections.get(grant.connection_ref).target,
                expected_revision=grant.connection_revision,
            ),
        )


def _profile(*, installation: str = "c") -> DshProfileBuildSnapshot:
    return DshProfileBuildSnapshot.model_validate(
        {
            "projection": {
                "profile": "web",
                "bundles": [
                    "@deepseek-ai/dsh-base",
                    "@kingsoftcloud/dsh-platform-resources",
                    "@kingsoftcloud/dsh-knowledge",
                    "@kingsoftcloud/dsh-memory",
                ],
                "configDigest": "sha256:" + "a" * 64,
                "configBytes": 128,
                "hostVersion": "0.1.1-rc.2",
            },
            "dependencyLockDigest": "sha256:" + "b" * 64,
            "installationDigest": "sha256:" + installation * 64,
        }
    )


def _studio(tmp_path: Path) -> tuple[StudioService, _Authority]:
    studio = StudioService(tmp_path)
    studio.credentials.put_session("RESOURCE_AK", "fixture-access-key")
    studio.credentials.put_session("RESOURCE_SK", "fixture-secret-key")
    studio.resource_connections.save(
        ResourceConnectionDeclaration.model_validate(
            {
                "label": "Knowledge",
                "target": {
                    "connectionRef": "connection-a",
                    "tenantRef": "tenant-a",
                    "principalRef": "principal-a",
                    "endpoint": "https://knowledge.example.test",
                    "authMode": "signed",
                },
                "credentials": {
                    "accessKeyRef": "env://RESOURCE_AK",
                    "secretKeyRef": "env://RESOURCE_SK",
                },
            }
        ),
        expected_revision=0,
    )
    authority = _Authority(studio.resource_connections)
    studio.resource_authority = authority
    studio.dsh_capabilities.capture_resource_build_snapshot = _profile
    studio.resource_dsh_capabilities.capture_resource_build_snapshot = _profile
    return studio, authority


def _draft(studio: StudioService):
    return studio.create_studio_agent(
        agent_id="resource-agent",
        name="Resource Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/resource-agent/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Search the bound knowledge base."),
            bindings=AgentBindings(plugins=[binding()]),
            security=SecuritySpec(network=NetworkPolicy(mode="open")),
        ),
    )


def _memory_draft(studio: StudioService):
    return studio.create_studio_agent(
        agent_id="memory-agent",
        name="Memory Agent",
        spec=AgentSpec(
            runtime=RuntimeRef(
                type="langgraph",
                project_path="agents/memory-agent/source",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            instructions=Instructions(system="Use the bound memory."),
            bindings=AgentBindings(plugins=[binding("memory-instance")]),
            memory=MemorySpec(
                enabled=True,
                provider_ref="binding://binding-a",
                scopes=["user"],
                recall={"minScore": 0},
                write=MemoryWriteSpec(mode="explicit_only"),
            ),
            context={"rollout": {"memoryWrite": "enabled"}},
            security=SecuritySpec(network=NetworkPolicy(mode="open")),
        ),
    )


def test_formal_build_embeds_verified_resource_snapshot_without_secrets(tmp_path: Path) -> None:
    studio, authority = _studio(tmp_path)
    draft = _draft(studio)

    build = studio._build_agent_bundle(draft)

    assert authority.calls == [("binding-a", 1)]
    assert build.resource_build_digest and build.resource_snapshot_digest
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    bundle_root = archive.parent / "agent-bundle"
    manifest = json.loads((bundle_root / "manifest.json").read_text())
    assert manifest["resourceBuildDigest"] == build.resource_build_digest
    assert manifest["resourceSnapshotDigest"] == build.resource_snapshot_digest
    reference = ResourceBuildReference(
        digest=build.resource_build_digest,
        snapshot_digest=build.resource_snapshot_digest,
    )
    resource_manifest, packages = restore_resource_build(
        bundle_root / "platform-resources",
        reference,
        cache_directory=tmp_path / "resource-cache",
    )
    frozen = resource_manifest.snapshot.bindings[0]
    assert frozen.connection_revision == 1
    assert frozen.connection.tenant_ref == "tenant-a"
    assert frozen.config.binding.resource.id == "resource-a"
    assert packages == {}
    serialized = "\n".join(
        path.read_text(errors="ignore")
        for path in bundle_root.rglob("*")
        if path.is_file()
    )
    assert "fixture-access-key" not in serialized
    assert "fixture-secret-key" not in serialized


def test_resource_snapshot_changes_build_identity(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)
    first = studio._build_agent_bundle(draft)
    studio.resource_dsh_capabilities.capture_resource_build_snapshot = lambda: _profile(
        installation="d"
    )

    second = studio._build_agent_bundle(draft)

    assert second.id != first.id
    assert second.resource_snapshot_digest != first.resource_snapshot_digest


def test_direct_builder_cannot_bypass_resource_admission(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)

    with pytest.raises(StudioError) as captured:
        studio.builder.build(draft)

    assert captured.value.code == "RESOURCE_BUILD_ADMISSION_REQUIRED"
    assert not list((tmp_path / "dist" / "resource-agent").glob("build_*"))


def test_connection_revision_drift_publishes_no_build(tmp_path: Path) -> None:
    studio, _ = _studio(tmp_path)
    draft = _draft(studio)
    original = studio.resource_connections.get("connection-a")

    class _DriftingAuthority(_Authority):
        def admit(self, config, *, expected_connection_revision=None):
            grant = super().admit(
                config,
                expected_connection_revision=expected_connection_revision,
            )
            studio.resource_connections.save(
                ResourceConnectionDeclaration.model_validate(
                    {
                        **original.model_dump(exclude={"revision"}),
                        "credentials": {
                            "accessKeyRef": "env://RESOURCE_AK_NEXT",
                            "secretKeyRef": "env://RESOURCE_SK_NEXT",
                        },
                    }
                ),
                expected_revision=1,
            )
            return grant

    studio.credentials.put_session("RESOURCE_AK_NEXT", "fixture-next-access")
    studio.credentials.put_session("RESOURCE_SK_NEXT", "fixture-next-secret")
    studio.resource_authority = _DriftingAuthority(studio.resource_connections)

    with pytest.raises(StudioError) as captured:
        studio._build_agent_bundle(draft)

    assert captured.value.code == "RESOURCE_CONNECTION_CHANGED"
    assert not list((tmp_path / "dist" / "resource-agent").glob("build_*"))


class _DshRuntime:
    def __init__(self) -> None:
        self.initialization = None
        self.write_authorizer = None
        self.deactivated: list[str] = []
        self.connector = DshMcpConnectorLease(
            endpoint="http://127.0.0.1:43210/mcp",
            profile="web",
            profile_digest="sha256:" + "a" * 64,
            descriptor_digest="sha256:" + "d" * 64,
            _bearer_token="fixture-core-root",
        )

    async def prepare_resource_generation(self, expected):
        assert expected == _profile()
        return SimpleNamespace(profile_digest=self.connector.profile_digest), "generation-a"

    async def activate_resources(self, initialization, *, expected, write_authorizer=None):
        assert expected == _profile()
        self.initialization = initialization
        self.write_authorizer = write_authorizer
        leases = tuple(
            ResourceLease(f"opaque-{index}", scope, time.monotonic() + 600)
            for index, scope in enumerate(initialization.scopes)
        )
        return ActiveResources(
            activation_id=initialization.scopes[0].activation_id,
            socket_path=Path("/tmp/fixture-resource.sock"),
            leases=leases,
            worker_pid=4321,
        )

    async def resource_connector_lease(self, active):
        assert active.activation_id == self.initialization.scopes[0].activation_id
        return self.connector

    async def deactivate_resources(self, activation_id):
        self.deactivated.append(activation_id)


def _runtime_bundle(studio: StudioService, build) -> ResolvedPluginBundle:
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    root = archive.parent / "agent-bundle"
    manifest = BundleManifest.model_validate_json((root / "manifest.json").read_bytes())
    profile = CompositionProfile.model_validate(
        {
            "agentProvider": {
                "ref": "plugin://io.ksadk.harness-provider@1.0.0",
            },
            "capabilities": [
                {
                    "ref": PLATFORM_RESOURCE_MCP_REF,
                    "config": {"artifactPath": "platform-resources"},
                }
            ],
        }
    )
    composition = PluginRegistry(
        [legacy_harness_agent_provider_manifest(), platform_resource_mcp_manifest()]
    ).resolve(profile)
    return ResolvedPluginBundle(
        root=root,
        manifest=manifest,
        resolved_agent_spec=json.loads((root / "resolved-agent-spec.json").read_text()),
        composition=composition,
    )


@pytest.mark.asyncio
async def test_runtime_reauthorizes_worker_and_releases_scoped_mcp(tmp_path: Path) -> None:
    studio, authority = _studio(tmp_path)
    build = studio._build_agent_bundle(_draft(studio))
    bundle = _runtime_bundle(studio, build)
    dsh = _DshRuntime()
    runtime = PlatformResourceMCPRuntime(
        profile=bundle.composition.profile,
        authority=authority,
        connections=studio.resource_connections,
        dsh_service=dsh,
        actor_ref="local-user",
        state_root=tmp_path / "runtime-state",
    )
    await runtime.start()

    specs = await runtime.activation_mcp_specs(bundle, activation_key="session-a")

    assert authority.calls == [("binding-a", 1), ("binding-a", 1)]
    assert len(specs) == 1
    assert specs[0].name == "platform-resources"
    assert specs[0].tool_filter == ("search_knowledge_base",)
    assert specs[0].api_key.startswith("ks2.")
    initialization = dsh.initialization
    assert initialization.resource_snapshot.digest == build.resource_snapshot_digest
    assert initialization.scopes[0].identity.actor_ref == "local-user"
    assert initialization.scopes[0].allowed_operations == ("search_knowledge_base",)
    assert initialization.bindings[0].access_key.get_secret_value() == "fixture-access-key"
    assert "fixture-access-key" not in repr(initialization)

    await runtime.release_activation_mcp_specs("session-a", specs)
    assert dsh.deactivated == [initialization.scopes[0].activation_id]
    await runtime.dispose()


@pytest.mark.asyncio
async def test_runtime_exposes_memory_write_only_with_policy_and_host_authorizer(
    tmp_path: Path,
) -> None:
    from ksadk.resource_runtime.policy_authorization import FullAccessResourceWriteAuthorizer

    studio, authority = _studio(tmp_path)
    build = studio._build_agent_bundle(_memory_draft(studio))
    bundle = _runtime_bundle(studio, build)
    dsh = _DshRuntime()
    runtime = PlatformResourceMCPRuntime(
        profile=bundle.composition.profile,
        authority=authority,
        connections=studio.resource_connections,
        dsh_service=dsh,
        actor_ref="local-user",
        state_root=tmp_path / "runtime-state",
        write_authorizer_factory=lambda activation_key, _bundle: (
            FullAccessResourceWriteAuthorizer(activation_key=activation_key)
        ),
    )
    await runtime.start()

    specs = await runtime.activation_mcp_specs(bundle, activation_key="session-a")

    assert specs[0].tool_filter == ("load_memory", "save_memory", "memory_status")
    assert dsh.initialization.scopes[0].allowed_operations == specs[0].tool_filter
    assert isinstance(dsh.write_authorizer, FullAccessResourceWriteAuthorizer)
    await runtime.release_activation_mcp_specs("session-a", specs)
    await runtime.dispose()


@pytest.mark.asyncio
async def test_runtime_keeps_memory_read_only_without_run_authorization(tmp_path: Path) -> None:
    studio, authority = _studio(tmp_path)
    build = studio._build_agent_bundle(_memory_draft(studio))
    bundle = _runtime_bundle(studio, build)
    dsh = _DshRuntime()
    runtime = PlatformResourceMCPRuntime(
        profile=bundle.composition.profile,
        authority=authority,
        connections=studio.resource_connections,
        dsh_service=dsh,
        actor_ref="local-user",
        state_root=tmp_path / "runtime-state",
    )
    await runtime.start()

    specs = await runtime.activation_mcp_specs(bundle, activation_key="session-a")

    assert specs[0].tool_filter == ("load_memory",)
    assert dsh.initialization.scopes[0].allowed_operations == ("load_memory",)
    assert dsh.write_authorizer is None
    await runtime.release_activation_mcp_specs("session-a", specs)
    await runtime.dispose()


def test_composed_resource_build_selects_platform_mcp_capability(tmp_path: Path) -> None:
    studio, _authority = _studio(tmp_path)
    source = _draft(studio)
    draft = source.model_copy(
        update={
            "spec": source.spec.model_copy(
                update={"runtime": RuntimeRef(type="harness")}
            )
        }
    )
    manifest = legacy_harness_agent_provider_manifest()
    reference = f"plugin://{manifest.metadata.id}@{manifest.metadata.version}"
    studio.plugin_compositions.replace_provider_registrations({reference: manifest})

    composition = studio.plugin_compositions.compile(draft)

    assert any(
        capability.ref == PLATFORM_RESOURCE_MCP_REF
        for capability in composition.profile.capabilities
    )


def test_composed_memory_resource_materializes_its_memory_provider_owner(tmp_path: Path) -> None:
    studio, _authority = _studio(tmp_path)
    source = _draft(studio)
    memory_binding = binding("memory-instance")
    draft = source.model_copy(
        update={
            "spec": source.spec.model_copy(
                update={
                    "runtime": RuntimeRef(type="harness"),
                    "bindings": AgentBindings(plugins=[memory_binding]),
                    "memory": MemorySpec(
                        enabled=True,
                        provider_ref="binding://binding-a",
                        scopes=["user"],
                        write={"mode": "off"},
                    ),
                }
            )
        }
    )
    manifest = legacy_harness_agent_provider_manifest()
    reference = f"plugin://{manifest.metadata.id}@{manifest.metadata.version}"
    studio.plugin_compositions.replace_provider_registrations({reference: manifest})

    composition = studio.plugin_compositions.compile(draft)

    selected = [
        capability
        for capability in composition.profile.capabilities
        if capability.ref == PLATFORM_RESOURCE_MCP_REF
    ]
    assert len(selected) == 1
    assert selected[0].config == {
        "artifactPath": "platform-resources",
        "providerRef": "binding://binding-a",
        "scopes": ["user"],
    }
