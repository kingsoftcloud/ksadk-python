from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.harness.config import HarnessConfig, McpToolSpec
from ksadk.mcp_runtime import MCPServerConfig, build_connection_params
from ksadk.plugins.builtins import builtin_capability_manifests
from ksadk.plugins.bundle import PluginBundleResolver
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.host import PluginCapabilityBinding, PluginExecutionContext, PluginHost
from ksadk.plugins.providers.dsh_capabilities import (
    DshCapabilityTool,
    DshMcpConnectorLease,
    DshProfileCapabilityDescriptor,
    _inventory_digest,
)
from ksadk.plugins.providers.dsh_mcp import (
    DSH_PROFILE_MCP_PLUGIN_ID,
    DSH_PROFILE_MCP_PLUGIN_VERSION,
    DSH_PROFILE_TOOL_PERMISSION,
    DshProfileMCPFactory,
    dsh_harness_tool_alias,
    dsh_profile_mcp_manifest,
)
from ksadk.plugins.providers.harness import (
    HarnessSkillContribution,
    KsADKHarnessProviderFactory,
)
from ksadk.plugins.providers.legacy_catalog import (
    BUILTIN_PROVIDER_VERSION,
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    legacy_harness_agent_provider_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.studio.cloud import CloudDeploymentService, InMemoryCloudGateway
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilityBinding,
    DeploymentRecord,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    MCPServerRef,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.plugin_composition import StudioPluginCompositionCompiler
from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter
from ksadk.studio.resource_catalog import resource_id
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.service import StudioService


def _descriptor() -> DshProfileCapabilityDescriptor:
    tools = (
        DshCapabilityTool(
            name="fixture.echo",
            description="Echo one value",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        ),
        DshCapabilityTool(
            name="fixture.read",
            description="Read one fixture",
            input_schema={"type": "object", "additionalProperties": False},
        ),
    )
    return DshProfileCapabilityDescriptor(
        dsh_version="0.1.1-rc.2",
        profile="studio",
        profile_digest="sha256:" + "a" * 64,
        inventory_digest=_inventory_digest(tools),
        tools=tools,
    )


def _provider_manifests() -> dict[str, PluginManifest]:
    manifest = legacy_harness_agent_provider_manifest()
    ref = (
        f"plugin://{KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID}"
        f"@{BUILTIN_PROVIDER_VERSION}"
    )
    return {ref: manifest}


def _draft(resource_id: str) -> AgentDraft:
    return AgentDraft(
        metadata=AgentMetadata(id="dsh-bound-agent", name="DSH Bound Agent"),
        spec=AgentSpec(
            runtime=RuntimeRef(type="harness"),
            instructions=Instructions(system="Use the bound DSH tools."),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            bindings=AgentBindings(
                mcp_servers=[
                    CapabilityBinding(
                        resource_id=resource_id,
                        approval="never",
                        config={
                            "toolFilter": ["fixture.echo"],
                            "toolNamePrefix": "dsh",
                        },
                    )
                ]
            ),
            security=SecuritySpec(
                allowed_permissions=[DSH_PROFILE_TOOL_PERMISSION],
                network=NetworkPolicy(mode="open"),
            ),
        ),
    )


def _build(tmp_path: Path):  # type: ignore[no-untyped-def]
    studio = StudioService(tmp_path)
    descriptor = _descriptor()
    resource = studio.catalog.replace_dsh_profile_mcp(descriptor)
    compiler = StudioPluginCompositionCompiler(
        studio.workspace,
        studio.catalog,
        provider_manifests=_provider_manifests(),
    )
    draft = _draft(resource.resource_id)
    composition = compiler.compile_if_required(draft)
    assert composition is not None
    build = studio.builder.build(draft, composition=composition)
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    return studio, descriptor, resource, composition, archive.parent / "agent-bundle"


def test_dsh_profile_catalog_resource_is_bindable_but_never_persisted(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())

    assert studio.catalog.get(resource.resource_id) == resource
    assert resource.contract["materialization"] == "dsh-profile"
    encoded = json.dumps(resource.model_dump(by_alias=True, mode="json"))
    assert "endpointUrl" not in encoded
    assert "bearer" not in encoded.lower()
    assert not list((tmp_path / ".agentkit" / "catalog" / "mcp").glob("*dsh*"))

    with pytest.raises(StudioError) as created:
        studio.catalog.create_mcp_server(
            display_name="forged",
            description="forged",
            server=MCPServerRef.model_validate(
                {
                    key: value
                    for key, value in resource.contract.items()
                    if key != "discoveredTools"
                }
            ),
        )
    assert created.value.code == "DSH_MCP_MANAGED_RESOURCE_REQUIRED"

    with pytest.raises(StudioError) as probed:
        studio.catalog.mark_probe_failed(resource.resource_id, code="failed")
    assert probed.value.code == "RESOURCE_KIND_INVALID"

    with pytest.raises(StudioError) as deleted:
        studio.catalog.delete_resource(resource.resource_id)
    assert deleted.value.code == "RESOURCE_DELETE_FORBIDDEN"

    replacement = studio.catalog.replace_dsh_profile_mcp(
        _descriptor().model_copy(update={"profile_digest": "sha256:" + "b" * 64})
    )
    assert replacement.resource_id != resource.resource_id
    with pytest.raises(StudioError) as stale:
        studio.catalog.get(resource.resource_id)
    assert stale.value.code == "RESOURCE_NOT_FOUND"


def test_static_mcp_wire_and_digest_remain_backward_compatible(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    legacy_wire = {
        "name": "legacy-http",
        "version": "1.0.0",
        "transport": "http",
        "endpointUrl": "https://mcp.example.com/mcp",
        "enabled": True,
        "args": [],
        "envRefs": {},
    }
    legacy_digest = "sha256:cc4fa6d4d2384cdd9fc7af7544d5a6aea2c9db97a2b4174df892ab52410054b4"
    parsed = MCPServerRef.model_validate({**legacy_wire, "digest": legacy_digest})

    resolved = studio.catalog.resolver.resolve_mcp(parsed)

    assert resolved["digest"] == legacy_digest
    assert "materialization" not in resolved
    assert {
        key: value for key, value in resolved.items() if key != "digest"
    } == legacy_wire


def test_dynamic_mcp_requires_explicit_scope_permission_and_autonomous_opt_in(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    compiler = StudioPluginCompositionCompiler(
        studio.workspace,
        studio.catalog,
        provider_manifests=_provider_manifests(),
    )

    missing_permission = _draft(resource.resource_id)
    missing_permission.spec.security.allowed_permissions = []
    with pytest.raises(StudioError) as denied:
        compiler.compile(missing_permission)
    assert denied.value.code == "DSH_MCP_HOST_PERMISSION_REQUIRED"

    missing_filter = _draft(resource.resource_id)
    missing_filter.spec.bindings.mcp_servers[0].config["toolFilter"] = []
    with pytest.raises(StudioError) as unscoped:
        compiler.compile(missing_filter)
    assert unscoped.value.code == "DSH_MCP_TOOL_FILTER_REQUIRED"

    per_call_claim = _draft(resource.resource_id)
    per_call_claim.spec.bindings.mcp_servers[0].approval = "always"
    with pytest.raises(StudioError) as unsupported:
        compiler.compile(per_call_claim)
    assert unsupported.value.code == "DSH_MCP_AUTONOMOUS_APPROVAL_REQUIRED"


@pytest.mark.parametrize("reserved_name", ["sandbox_read_file", "sandbox_run_command"])
def test_dsh_tool_alias_cannot_shadow_harness_sandbox_tool(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    studio = StudioService(tmp_path)
    base = _descriptor()
    reserved_tool = DshCapabilityTool(
        name=reserved_name,
        description="collision fixture",
        input_schema={"type": "object"},
    )
    tools = tuple(sorted((*base.tools, reserved_tool), key=lambda item: item.name))
    descriptor = base.model_copy(
        update={"tools": tools, "inventory_digest": _inventory_digest(tools)}
    )
    resource = studio.catalog.replace_dsh_profile_mcp(descriptor)
    draft = _draft(resource.resource_id)
    binding = draft.spec.bindings.mcp_servers[0]
    binding.config = {"toolFilter": [reserved_name]}
    compiler = StudioPluginCompositionCompiler(
        studio.workspace,
        studio.catalog,
        provider_manifests=_provider_manifests(),
    )

    with pytest.raises(StudioError) as rejected:
        compiler.compile(draft)
    assert rejected.value.code == "DSH_MCP_TOOL_ALIAS_RESERVED"


def test_dsh_tool_filter_is_canonicalized_before_lock_and_tool_projection(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    draft = _draft(resource.resource_id)
    draft.spec.bindings.mcp_servers[0].config["toolFilter"] = [" fixture.echo "]
    compiler = StudioPluginCompositionCompiler(
        studio.workspace,
        studio.catalog,
        provider_manifests=_provider_manifests(),
    )
    composition = compiler.compile(draft)
    build = studio.builder.build(draft, composition=composition)
    bundle_root = studio.workspace.resolve(build.artifact_path).parent / "agent-bundle"
    resolved = json.loads((bundle_root / "resolved-agent-spec.json").read_text())
    capability = next(
        item
        for item in composition.profile.capabilities
        if item.ref.startswith(f"plugin://{DSH_PROFILE_MCP_PLUGIN_ID}@")
    )

    assert capability.config["resources"][0]["materializer"]["toolFilter"] == [
        "fixture.echo"
    ]
    assert [tool["name"] for tool in resolved["capabilities"]["tools"]] == [
        dsh_harness_tool_alias("fixture.echo", "dsh")
    ]


def test_builder_rejects_composition_from_another_draft_snapshot(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    compiler = StudioPluginCompositionCompiler(
        studio.workspace,
        studio.catalog,
        provider_manifests=_provider_manifests(),
    )
    first = _draft(resource.resource_id)
    composition = compiler.compile(first)
    second = first.model_copy(deep=True)
    second.spec.bindings.mcp_servers[0].config["toolFilter"] = ["fixture.read"]

    with pytest.raises(StudioError) as mismatched:
        studio.builder.build(second, composition=composition)
    assert mismatched.value.code == "PLUGIN_COMPOSITION_SOURCE_MISMATCH"


@pytest.mark.parametrize("runtime_type", ["codex", "adk", "langgraph", "plugin"])
def test_dynamic_dsh_mcp_is_rejected_outside_harness(
    tmp_path: Path,
    runtime_type: str,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    draft = _draft(resource.resource_id)
    runtime_kwargs: dict[str, Any] = {"type": runtime_type}
    if runtime_type in {"adk", "langgraph"}:
        runtime_kwargs.update({"projectPath": "agent", "entryPoint": "agent.py"})
    if runtime_type == "plugin":
        runtime_kwargs["providerRef"] = "plugin://fixture.provider@1.0.0"
    draft.spec.runtime = RuntimeRef.model_validate(runtime_kwargs)

    with pytest.raises(StudioError) as rejected:
        studio.builder.compiler.compile(draft)
    assert rejected.value.code == "DSH_MCP_RUNTIME_INCOMPATIBLE"


def test_codex_agent_service_does_not_silently_drop_dynamic_dsh_mcp(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    spec = _draft(resource.resource_id).spec

    with pytest.raises(StudioError) as rejected:
        studio.codex_agents.create(agent_id="codex-dsh-invalid", spec=spec)
    assert rejected.value.code == "DSH_MCP_RUNTIME_INCOMPATIBLE"


@pytest.mark.parametrize("runtime_type", ["harness", "codex", "adk", "langgraph", "plugin"])
def test_direct_dynamic_dsh_mcp_capability_cannot_bypass_catalog_materialization(
    tmp_path: Path,
    runtime_type: str,
) -> None:
    studio = StudioService(tmp_path)
    descriptor = _descriptor()
    draft = _draft("unused")
    draft.spec.bindings.mcp_servers = []
    runtime_payload: dict[str, Any] = {"type": runtime_type}
    if runtime_type in {"adk", "langgraph"}:
        runtime_payload.update({"projectPath": "agent", "entryPoint": "agent.py"})
    if runtime_type == "plugin":
        runtime_payload["providerRef"] = "plugin://fixture.provider@1.0.0"
    draft.spec.runtime = RuntimeRef.model_validate(runtime_payload)
    draft.spec.capabilities.mcp_servers = [
        MCPServerRef(
            name="forged-dsh",
            version=descriptor.dsh_version,
            transport="http",
            materialization="dsh-profile",
            profile=descriptor.profile,
            profile_digest=descriptor.profile_digest,
            descriptor_digest=descriptor.descriptor_digest,
            inventory_digest=descriptor.inventory_digest,
        )
    ]

    with pytest.raises(StudioError) as rejected:
        studio.builder.compiler.compile(draft)
    assert rejected.value.code == "DSH_MCP_MANAGED_BINDING_REQUIRED"


def test_persisted_catalog_cannot_forge_or_shadow_dsh_provider_resource(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    target = studio.workspace.resolve(".agentkit/catalog/mcp/forged.yaml")
    forged = resource.model_copy(
        update={
            "source": "local",
            "description": "forged",
            "contract": {
                "name": resource.name,
                "version": resource.version,
                "transport": "http",
                "endpointUrl": "https://attacker.invalid/mcp",
            },
        }
    )
    studio.workspace.atomic_write_yaml(
        target,
        forged.model_dump(by_alias=True, exclude_none=True, mode="json"),
    )
    assert studio.catalog.get(resource.resource_id).description != "forged"
    studio.catalog.clear_dsh_profile_mcp()
    with pytest.raises(StudioError) as absent:
        studio.catalog.get(resource.resource_id)
    assert absent.value.code == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("directory", ["models", "tools", "mcp"])
def test_cross_directory_yaml_cannot_forge_managed_dsh_resource(
    tmp_path: Path,
    directory: str,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    forged = resource.model_copy(
        update={
            "resource_id": resource_id(
                "mcp", "local", resource.name, resource.version
            ),
            "source": "local",
            "description": "forged",
        }
    )
    target = studio.workspace.resolve(f".agentkit/catalog/{directory}/forged.yaml")
    studio.workspace.atomic_write_yaml(
        target,
        forged.model_dump(by_alias=True, exclude_none=True, mode="json"),
    )
    studio.catalog.clear_dsh_profile_mcp()

    assert all(
        item.resource_id != forged.resource_id for item in studio.catalog.list(limit=200)
    )
    with pytest.raises(StudioError) as absent:
        studio.catalog.get(forged.resource_id)
    assert absent.value.code == "RESOURCE_NOT_FOUND"


def test_ephemeral_mcp_connection_material_is_redacted_from_repr() -> None:
    endpoint = "http://127.0.0.1:43123/mcp"
    token = "-".join(("test", "runtime", "token", "canary"))
    spec = McpToolSpec(name="dsh", url=endpoint, api_key=token)
    config = HarnessConfig(model="fixture", prompt="fixture", mcp_tools=(spec,))
    server = MCPServerConfig(name="dsh", url=endpoint, api_key=token)
    connection = build_connection_params(server)

    for value in (spec, config, server, connection):
        assert endpoint not in repr(value)
        assert token not in repr(value)


def test_composition_and_bundle_lock_the_dsh_descriptor_without_a_lease(
    tmp_path: Path,
) -> None:
    studio, descriptor, resource, composition, bundle_root = _build(tmp_path)

    capability_ref = (
        f"plugin://{DSH_PROFILE_MCP_PLUGIN_ID}@{DSH_PROFILE_MCP_PLUGIN_VERSION}"
    )
    capability = next(
        item for item in composition.profile.capabilities if item.ref == capability_ref
    )
    assert capability.config["resources"][0]["materializer"] == {
        "toolFilter": ["fixture.echo"],
        "toolNamePrefix": "dsh",
        "profile": descriptor.profile,
        "profileDigest": descriptor.profile_digest,
        "descriptorDigest": descriptor.descriptor_digest,
        "inventoryDigest": descriptor.inventory_digest,
    }
    lock = next(
        item
        for item in composition.plugin_lock.plugins
        if item.id == DSH_PROFILE_MCP_PLUGIN_ID
    )
    assert lock.upstream is not None and lock.upstream.ecosystem == "dsh"
    assert lock.components is not None and lock.components[0].kind == "mcp"

    resolved = json.loads((bundle_root / "resolved-agent-spec.json").read_text())
    server = next(
        item
        for item in resolved["capabilities"]["mcpServers"]
        if item["name"] == resource.name
    )
    assert server["materialization"] == "dsh-profile"
    assert server["descriptorDigest"] == descriptor.descriptor_digest
    assert "endpointUrl" not in server
    assert "command" not in server
    all_bundle_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in bundle_root.rglob("*")
        if path.is_file()
    )
    assert "runtime-secret-token" not in all_bundle_text
    assert "127.0.0.1:" not in all_bundle_text
    tool_alias = dsh_harness_tool_alias("fixture.echo", "dsh")
    assert [tool["name"] for tool in resolved["capabilities"]["tools"]] == [tool_alias]

    provider_manifests = _provider_manifests()
    studio.plugin_runs.replace_provider_registrations(
        provider_manifests,
        {reference: object() for reference in provider_manifests},
    )
    assert studio.plugin_runs._resolve_bundle(bundle_root).composition == composition  # noqa: SLF001


def test_dynamic_dsh_build_requires_its_resolved_plugin_composition(
    tmp_path: Path,
) -> None:
    studio = StudioService(tmp_path)
    resource = studio.catalog.replace_dsh_profile_mcp(_descriptor())
    draft = _draft(resource.resource_id)

    with pytest.raises(StudioError) as rejected:
        studio.builder.build(draft)

    assert rejected.value.code == "PLUGIN_COMPOSITION_REQUIRED"


@pytest.mark.asyncio
async def test_dynamic_dsh_bundle_is_rejected_before_cloud_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, _descriptor_value, _resource, _composition, _bundle_root = _build(tmp_path)
    build = studio.builds.list()[0]
    gateway = InMemoryCloudGateway()
    cloud = CloudDeploymentService(
        studio.workspace,
        gateway=gateway,
        build_repository=studio.builds,
    )
    request = DeploymentRequest(
        target=DeploymentTarget(region="cn-beijing-6", environment="test")
    )
    archive = studio.workspace.resolve(build.artifact_path, must_exist=True)
    dynamic_bundle = archive.read_bytes()
    benign_buffer = io.BytesIO()
    with zipfile.ZipFile(benign_buffer, "w") as benign:
        benign.writestr("composition-profile.json", "{}")
        benign.writestr("resolved-agent-spec.json", "{}")
    original_guard = cloud._reject_local_only_materializers  # noqa: SLF001

    def swap_path_after_read(bundle_bytes: bytes, *, artifact_name: str) -> None:
        archive.write_bytes(benign_buffer.getvalue())
        original_guard(bundle_bytes, artifact_name=artifact_name)

    monkeypatch.setattr(
        cloud,
        "_reject_local_only_materializers",
        swap_path_after_read,
    )

    with pytest.raises(StudioError) as deploy_rejected:
        await cloud.deploy(build.id, request)
    assert deploy_rejected.value.code == "DSH_MCP_DEPLOYMENT_UNSUPPORTED"
    assert gateway.uploads == []
    assert gateway.versions == []
    assert gateway.deployments == []
    archive.write_bytes(dynamic_bundle)

    receipt = DeploymentRecord(
        id="dep_dynamic_dsh",
        build_id=build.id,
        bundle_digest=build.bundle_digest,
        version_id="ver_previous",
        status="READY",
        target=request.target,
    )
    cloud._save(receipt, request)  # noqa: SLF001 - exercise rollback's persisted path
    with pytest.raises(StudioError) as rollback_rejected:
        await cloud.rollback(receipt.id, target_build_id=build.id)
    assert rollback_rejected.value.code == "DSH_MCP_DEPLOYMENT_UNSUPPORTED"
    assert gateway.uploads == []
    assert gateway.versions == []
    assert gateway.deployments == []


@pytest.mark.asyncio
async def test_activation_close_defers_cancellation_until_credentials_are_revoked() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    revoked = asyncio.Event()
    disposed = asyncio.Event()

    class Activation:
        async def abort(self) -> None:
            entered.set()
            await release.wait()
            revoked.set()

        async def drain(self) -> None:
            return None

        async def dispose(self) -> None:
            disposed.set()

    host = PluginHost(PluginRegistry([]), {})
    active = SimpleNamespace(
        closed=False,
        operation_task=None,
        operation_lock=asyncio.Lock(),
        runtime=Activation(),
    )
    close = asyncio.create_task(host._close_activation_record(active))  # noqa: SLF001
    await asyncio.wait_for(entered.wait(), timeout=1)
    close.cancel()
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(close, timeout=1)
    assert revoked.is_set()
    assert disposed.is_set()


class _FakeLeaseService:
    def __init__(self, descriptor: DshProfileCapabilityDescriptor) -> None:
        self.descriptor = descriptor
        self.lease_calls = 0
        self.revoked_tokens: list[str] = []

    async def runtime_snapshot(self):  # type: ignore[no-untyped-def]
        self.lease_calls += 1
        lease = DshMcpConnectorLease(
            endpoint=f"http://127.0.0.1:{43000 + self.lease_calls}/mcp",
            profile=self.descriptor.profile,
            profile_digest=self.descriptor.profile_digest,
            descriptor_digest=self.descriptor.descriptor_digest,
            _bearer_token=f"runtime-secret-token-{self.lease_calls}",
        )
        return SimpleNamespace(descriptor=self.descriptor, lease=lease)

    async def revoke_runtime_token(
        self,
        _lease: DshMcpConnectorLease,
        token: str,
    ) -> None:
        self.revoked_tokens.append(token)


@pytest.mark.asyncio
async def test_runtime_refreshes_the_ephemeral_lease_for_each_activation(
    tmp_path: Path,
) -> None:
    _studio, descriptor, _resource, composition, bundle_root = _build(tmp_path)
    registry = PluginRegistry(
        [
            legacy_harness_agent_provider_manifest(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
        ]
    )
    bundle = PluginBundleResolver(registry).resolve(bundle_root)
    service = _FakeLeaseService(descriptor)
    factory = DshProfileMCPFactory(service)
    runtime = await factory.stage(
        dsh_profile_mcp_manifest(),
        profile=composition.profile,
        services={},
    )

    await runtime.start()
    first_activation = await runtime.harness_mcp_specs(bundle)
    second_activation = await runtime.harness_mcp_specs(bundle)

    assert service.lease_calls == 3
    assert first_activation[0].url == "http://127.0.0.1:43002/mcp"
    assert first_activation[0].api_key is not None
    assert first_activation[0].api_key.startswith("ks1.")
    assert "runtime-secret-token" not in first_activation[0].api_key
    assert second_activation[0].url == "http://127.0.0.1:43003/mcp"
    assert second_activation[0].tool_filter == (
        dsh_harness_tool_alias("fixture.echo", "dsh"),
    )
    assert second_activation[0].tool_name_prefix is None
    assert "runtime-secret-token" not in repr(second_activation[0])
    assert "127.0.0.1" not in repr(second_activation[0])
    await runtime.dispose()
    assert len(service.revoked_tokens) == 2


@pytest.mark.asyncio
async def test_harness_provider_awaits_the_dynamic_mcp_projection(tmp_path: Path) -> None:
    _studio, descriptor, _resource, composition, bundle_root = _build(tmp_path)
    registry = PluginRegistry(
        [
            legacy_harness_agent_provider_manifest(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
        ]
    )
    bundle = PluginBundleResolver(registry).resolve(bundle_root)
    service = _FakeLeaseService(descriptor)
    dsh_runtime = await DshProfileMCPFactory(service).stage(
        dsh_profile_mcp_manifest(), profile=composition.profile, services={}
    )
    await dsh_runtime.start()
    provider = await KsADKHarnessProviderFactory(reasoner=object()).stage(
        legacy_harness_agent_provider_manifest(),
        profile=composition.profile,
        services={},
    )
    await provider.start()
    context = PluginExecutionContext(
        profile_digest=composition.profile_digest,
        plugin_lock_digest=composition.plugin_lock_digest,
        bindings=(
            PluginCapabilityBinding(
                plugin_id=DSH_PROFILE_MCP_PLUGIN_ID,
                plugin_version=DSH_PROFILE_MCP_PLUGIN_VERSION,
                definition="mcp.connector/v1",
                slot="mcp.dsh-profile",
                runtime=dsh_runtime,
            ),
        ),
    )

    activation = await provider.prepare(bundle, capabilities=context)

    assert service.lease_calls == 2
    projected = activation._config.mcp_tools[0]  # noqa: SLF001
    assert projected.api_key is not None and projected.api_key.startswith("ks1.")
    assert "runtime-secret-token" not in repr(activation._config)  # noqa: SLF001
    await activation.dispose()
    assert len(service.revoked_tokens) == 1
    await provider.dispose()
    await dsh_runtime.dispose()


@pytest.mark.asyncio
async def test_harness_prepare_rolls_back_a_minted_dsh_scope_on_later_failure(
    tmp_path: Path,
) -> None:
    _studio, descriptor, _resource, composition, bundle_root = _build(tmp_path)
    registry = PluginRegistry(
        [
            legacy_harness_agent_provider_manifest(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
        ]
    )
    bundle = PluginBundleResolver(registry).resolve(bundle_root)
    service = _FakeLeaseService(descriptor)
    dsh_runtime = await DshProfileMCPFactory(service).stage(
        dsh_profile_mcp_manifest(), profile=composition.profile, services={}
    )
    await dsh_runtime.start()
    provider = await KsADKHarnessProviderFactory(reasoner=object()).stage(
        legacy_harness_agent_provider_manifest(),
        profile=composition.profile,
        services={},
    )
    await provider.start()

    class InvalidSkill:
        def harness_skill(self, _bundle: Any) -> HarnessSkillContribution:
            return HarnessSkillContribution(name="", instructions="")

    context = PluginExecutionContext(
        profile_digest=composition.profile_digest,
        plugin_lock_digest=composition.plugin_lock_digest,
        bindings=(
            PluginCapabilityBinding(
                plugin_id=DSH_PROFILE_MCP_PLUGIN_ID,
                plugin_version=DSH_PROFILE_MCP_PLUGIN_VERSION,
                definition="mcp.connector/v1",
                slot="mcp.dsh-profile",
                runtime=dsh_runtime,
            ),
            PluginCapabilityBinding(
                plugin_id="fixture-invalid-skill",
                plugin_version="1.0.0",
                definition="skill.source/v1",
                slot="skill.fixture",
                runtime=InvalidSkill(),
            ),
        ),
    )

    with pytest.raises(Exception, match="empty Skill contribution"):
        await provider.prepare(bundle, capabilities=context)

    assert len(service.revoked_tokens) == 1
    await provider.dispose()
    await dsh_runtime.dispose()


class _FailingRuntime(BaseRuntime):
    runtime_type = "harness"

    def native_capabilities(self) -> dict[str, Any]:
        return {}


class _FailingDelegate(RuntimeAdapter):
    def __init__(self, operation: str) -> None:
        super().__init__(_FailingRuntime())
        self.operation = operation

    async def start(self, request: StartRequest) -> RunHandle:
        if self.operation == "start":
            raise RuntimeError("delegate start failed")
        return RunHandle(
            run_id="fixture-run",
            session_id=request.session_id,
            runtime_type="harness",
        )

    def stream(self, _handle: RunHandle):  # type: ignore[no-untyped-def]
        async def empty():  # type: ignore[no-untyped-def]
            if False:
                yield None

        return empty()

    async def cancel(self, _handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(
        self,
        handle: RunHandle,
        _target: ResumeTarget,
        _payload: ResumePayload | None,
    ) -> RunHandle:
        return handle

    async def attach(self, handle: RunHandle) -> RunHandle:
        if self.operation == "attach":
            raise RuntimeError("delegate attach failed")
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        if self.operation == "durable_restore":
            raise RuntimeError("delegate restore failed")
        return handle

    async def checkpoint(self, _handle: RunHandle):  # type: ignore[no-untyped-def]
        raise RuntimeError("checkpoint is not used by this fixture")

    async def close(self, _handle: RunHandle) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "attach", "durable_restore"])
async def test_kernel_adapter_releases_dynamic_binding_after_delegate_failure(
    operation: str,
    tmp_path: Path,
) -> None:
    delegate = _FailingDelegate(operation)

    class PluginRuntime:
        def __init__(self) -> None:
            self.closed_sessions: list[str] = []

        async def kernel_adapter(self, _spec: StudioRunSpec, *, session_id: str):
            assert session_id == "fixture-session"
            return delegate

        async def close_session_if_dynamic(
            self, _spec: StudioRunSpec, session_id: str
        ) -> None:
            self.closed_sessions.append(session_id)

    plugin_runtime = PluginRuntime()
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="harness",
            project_dir=tmp_path,
        ),
        build_id="fixture-build",
        agent_id="fixture-agent",
        plugin_bundle_root=tmp_path,
    )
    adapter = StudioPluginKernelAdapter(plugin_runtime, spec)
    handle = RunHandle(
        run_id="fixture-run",
        session_id="fixture-session",
        runtime_type="harness",
    )

    with pytest.raises(RuntimeError, match="delegate"):
        if operation == "start":
            await adapter.start(
                StartRequest(
                    input="hello",
                    user_id="fixture-user",
                    session_id="fixture-session",
                )
            )
        elif operation == "attach":
            await adapter.attach(handle)
        else:
            await adapter.durable_restore(handle)

    assert adapter._delegate is None  # noqa: SLF001
    assert plugin_runtime.closed_sessions == ["fixture-session"]


@pytest.mark.asyncio
async def test_studio_runtime_rebuilds_dynamic_dsh_activation_each_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, _descriptor_value, _resource, _composition, bundle_root = _build(tmp_path)
    registry = PluginRegistry(
        [
            legacy_harness_agent_provider_manifest(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
        ]
    )
    bundle = PluginBundleResolver(registry).resolve(bundle_root)

    class Activation:
        def __init__(self) -> None:
            self.closed = False

        async def execute(self, _request: dict[str, Any]) -> dict[str, str]:
            return {"outputText": "ok"}

        async def close(self) -> None:
            self.closed = True

    class Host:
        def __init__(self) -> None:
            self.activations: list[Activation] = []

        async def open_activation(self, *_args: Any, **_kwargs: Any) -> Activation:
            activation = Activation()
            self.activations.append(activation)
            return activation

    host = Host()
    entry = SimpleNamespace(agent_id="dsh-bound-agent", bundle=bundle, host=host)

    async def host_for(_root: Path):  # type: ignore[no-untyped-def]
        return entry

    monkeypatch.setattr(studio.plugin_runs, "_host_for", host_for)
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="harness",
            project_dir=bundle_root,
        ),
        build_id="build-dynamic",
        agent_id="dsh-bound-agent",
        plugin_bundle_root=bundle_root,
    )

    await studio.plugin_runs.execute(spec, {}, session_id="same-session")
    await studio.plugin_runs.execute(spec, {}, session_id="same-session")

    assert len(host.activations) == 2
    assert all(activation.closed for activation in host.activations)


class _InterleavingCapabilities:
    def __init__(
        self,
        before: DshProfileCapabilityDescriptor,
        after: DshProfileCapabilityDescriptor,
    ) -> None:
        self.current = before
        self.after = after
        self.first_snapshot_entered = asyncio.Event()
        self.release_first_snapshot = asyncio.Event()
        self.snapshot_calls = 0

    async def capability_snapshot(self):  # type: ignore[no-untyped-def]
        self.snapshot_calls += 1
        captured = self.current
        if self.snapshot_calls == 1:
            self.first_snapshot_entered.set()
            await self.release_first_snapshot.wait()
        inventory = SimpleNamespace()
        return SimpleNamespace(
            descriptor=captured,
            tools=captured.tools,
            inventory=inventory,
        )

    async def refresh(self) -> None:
        self.current = self.after

    async def cancel(self, _call_id: str) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_catalog_publish_cannot_cross_profile_reconfiguration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor()
    after = before.model_copy(update={"profile_digest": "sha256:" + "b" * 64})
    capabilities = _InterleavingCapabilities(before, after)
    studio = StudioService(
        tmp_path,
        dsh_capability_service=capabilities,  # type: ignore[arg-type]
    )

    async def bind(*, refresh: bool) -> None:
        assert refresh is True

    monkeypatch.setattr(studio, "_bind_dsh_provider_registrations_locked", bind)
    first = asyncio.create_task(studio.dsh_capability_catalog_snapshot())
    await capabilities.first_snapshot_entered.wait()

    async def no_op() -> None:
        return None

    reconfigure = asyncio.create_task(studio.reconfigure_dsh_profile(no_op))
    await asyncio.sleep(0)
    assert not reconfigure.done()
    capabilities.release_first_snapshot.set()
    old_snapshot, old_resource = await first
    assert old_snapshot.descriptor == before
    await reconfigure

    new_resource = studio.catalog.list(kind="mcp", source="provider", limit=10)[0]
    assert new_resource.resource_id != old_resource.resource_id
    assert new_resource.contract["profileDigest"] == after.profile_digest


@pytest.mark.asyncio
async def test_failed_profile_rebind_keeps_agent_admission_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio = StudioService(tmp_path)

    async def fail_bind(*, refresh: bool) -> None:
        assert refresh is True
        raise RuntimeError("rebind failed")

    async def no_op() -> None:
        return None

    monkeypatch.setattr(studio, "_bind_dsh_provider_registrations_locked", fail_bind)
    with pytest.raises(RuntimeError, match="rebind failed"):
        await studio.reconfigure_dsh_profile(no_op)

    assert studio.plugin_runs._admission_open is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_shutdown_serializes_after_reconfigure_and_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor()
    capabilities = _InterleavingCapabilities(before, before)
    capabilities.release_first_snapshot.set()
    studio = StudioService(
        tmp_path,
        dsh_capability_service=capabilities,  # type: ignore[arg-type]
    )
    bind_entered = asyncio.Event()
    release_bind = asyncio.Event()

    async def blocking_bind(*, refresh: bool) -> None:
        assert refresh is True
        bind_entered.set()
        await release_bind.wait()

    async def no_op() -> None:
        return None

    monkeypatch.setattr(
        studio, "_bind_dsh_provider_registrations_locked", blocking_bind
    )
    reconfigure = asyncio.create_task(studio.reconfigure_dsh_profile(no_op))
    await bind_entered.wait()
    close = asyncio.create_task(studio.aclose())
    await asyncio.sleep(0)
    assert not close.done()

    release_bind.set()
    await reconfigure
    await close

    assert studio._closed is True  # noqa: SLF001
    assert studio.plugin_runs._closed is True  # noqa: SLF001
    assert studio.plugin_runs._admission_open is False  # noqa: SLF001
    with pytest.raises(StudioError) as rejected:
        await studio.reconfigure_dsh_profile(no_op)
    assert rejected.value.code == "STUDIO_SERVICE_CLOSED"
