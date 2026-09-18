"""Hosted Agent Kernel bundle preflight stays local and content-addressed."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.resolver import PluginRegistry
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.cloud import CloudDeploymentService, DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.hosted_kernel import (
    AGENT_KERNEL_V1_CONTRACT_DIGEST,
    preflight_hosted_kernel_bundle,
)
from ksadk.studio.workspace import Workspace


def _build_hosted_bundle(tmp_path: Path, runtime_type: str = "langgraph"):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    source = workspace.root / "runtime"
    source.mkdir()
    (source / "agent.py").write_text("graph = object()\n", encoding="utf-8")
    record = AgentBundleBuilder(workspace).build(
        AgentDraft(
            metadata=AgentMetadata(id="studio-graph", name="Studio Graph"),
            spec=AgentSpec(
                instructions=Instructions(system="Be concise.", task="Reply OK."),
                model=ModelSpec(
                    model="glm-5.1",
                    endpoint_url="https://model.example.test/v1/chat/completions",
                    credential_ref="env://MODEL_API_KEY",
                ),
                runtime=RuntimeRef(
                    type=runtime_type,
                    project_path="runtime",
                    entry_point="agent.py",
                    agent_variable="graph",
                ),
                security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.test"])),
            ),
        )
    )
    archive = workspace.resolve(record.artifact_path or "", must_exist=True)
    return workspace, record, archive


def _build_plugin_hosted_bundle(tmp_path: Path):
    workspace = Workspace(tmp_path / "plugin-workspace")
    workspace.initialize()
    provider_ref = "plugin://io.ksadk.provider@1.0.0"
    draft = AgentDraft(
        metadata=AgentMetadata(id="plugin-agent", name="Plugin Agent"),
        spec=AgentSpec(
            instructions=Instructions(system="Be concise.", task="Reply OK."),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.test/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            runtime=RuntimeRef(type="plugin", provider_ref=provider_ref),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.test"])
            ),
        ),
    )
    provider = PluginManifest.model_validate(
        {
            "metadata": {"id": "io.ksadk.provider", "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.provider:factory",
                "provides": [
                    {
                        "definition": "agent.provider/v1",
                        "slot": "agent.execution",
                        "mode": "unique",
                    }
                ],
                "isolation": "process",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": ["agentkit.runtime/v1"],
                },
                "healthContract": "plugin.health/v1",
                "provenance": {"source": "builtin", "digest": "sha256:" + "1" * 64},
            },
        }
    )
    profile = CompositionProfile.model_validate({"agentProvider": {"ref": provider_ref}})
    composition = PluginRegistry([provider]).resolve(profile)
    composition = replace(
        composition,
        source_digest=sha256_digest(
            canonical_json(draft.model_dump(by_alias=True, exclude_none=True, mode="json"))
        ),
    )
    record = AgentBundleBuilder(workspace).build(draft, composition=composition)
    archive = workspace.resolve(record.artifact_path or "", must_exist=True)
    return workspace, record, archive


def test_builder_embeds_current_kernel_contract_requirement_in_the_uploaded_zip(tmp_path: Path):
    _workspace, record, archive = _build_hosted_bundle(tmp_path)

    checked = preflight_hosted_kernel_bundle(archive.read_bytes())

    assert checked.manifest["bundleDigest"] == record.bundle_digest
    assert checked.requirement["kernelContract"] == {
        "set": "agent-kernel/v1",
        "digest": AGENT_KERNEL_V1_CONTRACT_DIGEST,
    }
    assert checked.requirement["runtime"] == {
        "type": "langgraph",
        "entryPoint": "agent.py",
        "agentVariable": "graph",
        "launchConfig": "runtime/agentengine.yaml",
        "launchConfigSha256": checked.requirement["runtime"]["launchConfigSha256"],
    }
    assert checked.provenance["hostedKernel"]["requirementDigest"] == checked.requirement_digest
    assert checked.manifest["hostedKernelRequirementDigest"] == checked.requirement_digest


def test_harness_bundle_runtime_is_admitted_by_hosted_kernel(tmp_path: Path):
    _workspace, record, archive = _build_hosted_bundle(tmp_path, runtime_type="harness")

    checked = preflight_hosted_kernel_bundle(archive.read_bytes())

    assert checked.requirement["runtime"]["type"] == "harness"


def test_plugin_bundle_embeds_a_content_addressed_plugin_host_launch(tmp_path: Path) -> None:
    _workspace, record, archive = _build_plugin_hosted_bundle(tmp_path)

    checked = preflight_hosted_kernel_bundle(archive.read_bytes())

    assert checked.manifest["bundleDigest"] == record.bundle_digest
    assert checked.requirement["runtime"] == {
        "type": "plugin",
        "launchMode": "plugin-host",
        "compositionProfile": "composition-profile.json",
        "compositionProfileDigest": checked.manifest["compositionProfileDigest"],
        "pluginLock": "plugin-lock.json",
        "pluginLockDigest": checked.manifest["pluginLockDigest"],
        "providerRef": "plugin://io.ksadk.provider@1.0.0",
    }


def test_plugin_bundle_rejects_a_tampered_composition_before_upload(tmp_path: Path) -> None:
    _workspace, _record, archive = _build_plugin_hosted_bundle(tmp_path)
    with zipfile.ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    profile = json.loads(entries["composition-profile.json"])
    profile["policies"] = {"tampered": "true"}
    entries["composition-profile.json"] = json.dumps(
        profile,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    rewritten = io.BytesIO()
    with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in entries.items():
            target.writestr(name, content)

    with pytest.raises(StudioError) as raised:
        preflight_hosted_kernel_bundle(rewritten.getvalue())

    assert raised.value.code == "HOSTED_KERNEL_BUNDLE_INCOMPATIBLE"
    assert raised.value.details["reason"] == "plugin_profile_digest"


def test_local_preflight_pin_matches_the_frozen_kernel_contract_manifest() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    contract_manifest = json.loads(
        (repo_root / "contracts" / "agent-kernel" / "v1" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert contract_manifest["contract_set"] == "agent-kernel/v1"
    assert contract_manifest["aggregate_digest"] == AGENT_KERNEL_V1_CONTRACT_DIGEST


@pytest.mark.asyncio
async def test_cloud_deploy_rejects_stale_kernel_requirement_before_upload(tmp_path: Path):
    workspace, record, archive = _build_hosted_bundle(tmp_path)
    with zipfile.ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}

    requirement = json.loads(entries["hosted-kernel-requirements.json"])
    requirement["kernelContract"]["digest"] = "0" * 64
    entries["hosted-kernel-requirements.json"] = json.dumps(
        requirement,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    rewritten = io.BytesIO()
    with zipfile.ZipFile(rewritten, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in entries.items():
            target.writestr(name, content)
    archive.write_bytes(rewritten.getvalue())

    class NeverUpload:
        called = False

        def __init__(self, **_kwargs) -> None:
            pass

        async def upload(self, *_args, **_kwargs) -> str:
            type(self).called = True
            raise AssertionError("preflight must run before KS3 upload")

    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=object(),
        uploader_factory=NeverUpload,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    service = CloudDeploymentService(workspace, gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )

    with pytest.raises(StudioError) as raised:
        await service.deploy(record.id, request)

    assert raised.value.code == "HOSTED_KERNEL_BUNDLE_INCOMPATIBLE"
    assert "contract digest" in raised.value.message
    assert NeverUpload.called is False
