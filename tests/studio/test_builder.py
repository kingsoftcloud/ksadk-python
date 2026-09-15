from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ksadk.detection import FrameworkDetector, FrameworkType
from ksadk.plugins.contracts import (
    CompositionProfile,
    PluginLock,
    PluginManifest,
    plugin_lock_digest,
)
from ksadk.plugins.resolver import PluginRegistry, ResolvedComposition
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    BundleManifest,
    CapabilitiesSpec,
    CapabilityRef,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.workspace import Workspace


def _build_in(
    root: Path,
    *,
    credential_ref: str = "env://MODEL_API_KEY",
    allowed_permissions: list[str] | None = None,
    skills: list[CapabilityRef] | None = None,
    composition: ResolvedComposition | None = None,
):
    workspace = Workspace(root)
    workspace.initialize()
    draft = AgentDraft(
        metadata=AgentMetadata(id="demo-agent", name="Demo Agent"),
        spec=AgentSpec(
            instructions=Instructions(system="Only say OK.", task="Be concise."),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref=credential_ref,
            ),
            capabilities=CapabilitiesSpec(skills=skills or []),
            security=SecuritySpec(
                allowed_permissions=allowed_permissions or [],
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )
    if composition is not None:
        source_payload = draft.model_dump(by_alias=True, exclude_none=True, mode="json")
        composition = replace(
            composition,
            source_digest=sha256_digest(canonical_json(source_payload)),
        )
    record = AgentBundleBuilder(workspace).build(draft, composition=composition)
    archive = workspace.resolve(record.artifact_path or "")
    return workspace, record, archive


def _resolved_composition(
    *,
    provider_permissions: tuple[str, ...] = (),
    provider_kernel_api: str = ">=1,<2",
    provider_protocols: tuple[str, ...] = ("agentkit.runtime/v1",),
) -> ResolvedComposition:
    event_store = PluginManifest.model_validate(
        {
            "metadata": {"id": "io.ksadk.event-store", "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.event_store:factory",
                "provides": [
                    {
                        "definition": "session.event-store/v1",
                        "slot": "session.events",
                        "mode": "unique",
                    }
                ],
                "isolation": "process",
                "compatibility": {
                    "kernelApi": ">=1,<2",
                    "runtimeProtocols": ["agentkit.runtime/v1"],
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": "sha256:" + "2" * 64,
                },
            },
        }
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
                "requires": [{"definition": "session.event-store/v1", "version": ">=1,<2"}],
                "permissions": list(provider_permissions),
                "isolation": "process",
                "compatibility": {
                    "kernelApi": provider_kernel_api,
                    "runtimeProtocols": list(provider_protocols),
                },
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": "sha256:" + "1" * 64,
                },
            },
        }
    )
    profile = CompositionProfile.model_validate(
        {
            "agentProvider": {"ref": "plugin://io.ksadk.provider@1.0.0"},
            "capabilities": [],
        }
    )
    return PluginRegistry([provider, event_store]).resolve(profile)


def test_builder_creates_complete_bundle(tmp_path: Path):
    workspace, record, archive = _build_in(tmp_path / "one")

    assert record.status == "SUCCEEDED"
    assert record.resolved_digest.startswith("sha256:")
    assert record.bundle_digest.startswith("sha256:")
    assert archive.is_file()
    bundle_dir = archive.parent / "agent-bundle"
    assert (bundle_dir / "manifest.json").is_file()
    assert (bundle_dir / "resolved-agent-spec.json").is_file()
    assert (bundle_dir / "agentkit.lock").is_file()
    assert (bundle_dir / "plugin-lock.json").is_file()
    assert (bundle_dir / "compatibility-report.json").is_file()
    assert (bundle_dir / "sbom.spdx.json").is_file()
    assert (bundle_dir / "provenance.json").is_file()
    assert (bundle_dir / "checksums.txt").is_file()

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundleDigest"] == record.bundle_digest
    assert manifest["bundleFormat"] == "agentkit.bundle/v2"
    assert manifest["runtimeContract"] == "agentkit.runtime/v1"
    assert manifest["pluginLockDigest"].startswith("sha256:")
    # A 0.8.2-compatible Bundle does not have to opt into Phase 2 composition.
    # Its empty lock remains on the explicit legacy execution path.
    assert manifest["compositionMode"] == "legacy"
    assert "compositionProfileDigest" not in manifest
    assert not (bundle_dir / "composition-profile.json").exists()
    lock = PluginLock.model_validate_json(
        (bundle_dir / "plugin-lock.json").read_text(encoding="utf-8")
    )
    assert lock == PluginLock()
    assert manifest["pluginLockDigest"] == plugin_lock_digest(lock)
    assert {item["path"] for item in manifest["files"]} >= {
        "resolved-agent-spec.json",
        "agentkit.lock",
        "compatibility-report.json",
        "checksums.txt",
        "instructions/system.md",
    }
    report_bytes = (bundle_dir / "compatibility-report.json").read_bytes()
    report_entry = next(
        item for item in manifest["files"] if item["path"] == "compatibility-report.json"
    )
    assert report_entry == {
        "path": "compatibility-report.json",
        "sha256": f"sha256:{hashlib.sha256(report_bytes).hexdigest()}",
        "size": len(report_bytes),
    }
    assert workspace.relative(archive) == record.artifact_path


def test_bundle_manifest_normalizes_historical_v2_without_rewriting_it() -> None:
    """A real 0.8.2-shaped v2 manifest remains on the legacy path.

    ``compositionMode`` is emitted by new builds only.  Absence is a
    compatibility projection, never an instruction to send a historical
    framework bundle into PluginHost.
    """

    manifest = BundleManifest.model_validate(
        {
            "bundleFormat": "agentkit.bundle/v2",
            "agentId": "release-082-agent",
            "sourceRevision": 1,
            "resolvedDigest": "sha256:" + "a" * 64,
            "runtimeType": "langgraph",
            "files": [],
        }
    )

    assert manifest.composition_mode is None
    assert manifest.execution_profile == "legacy"


def test_composed_bundle_mode_requires_a_profile_digest() -> None:
    with pytest.raises(ValueError, match="compositionProfileDigest"):
        BundleManifest.model_validate(
            {
                "bundleFormat": "agentkit.bundle/v2",
                "agentId": "composed-agent",
                "sourceRevision": 1,
                "resolvedDigest": "sha256:" + "a" * 64,
                "compositionMode": "composed",
                "files": [],
            }
        )


def test_builder_is_byte_deterministic_across_workspaces(tmp_path: Path):
    _, first_record, first_archive = _build_in(tmp_path / "first")
    _, second_record, second_archive = _build_in(tmp_path / "second")

    assert first_record.id == second_record.id
    assert first_record.resolved_digest == second_record.resolved_digest
    assert first_record.bundle_digest == second_record.bundle_digest
    with zipfile.ZipFile(first_archive) as bundle:
        assert "compatibility-report.json" in bundle.namelist()
    assert (
        hashlib.sha256(first_archive.read_bytes()).digest()
        == hashlib.sha256(second_archive.read_bytes()).digest()
    )


def test_builder_embeds_resolved_composition_without_changing_legacy_shape(
    tmp_path: Path,
):
    composition = _resolved_composition()
    _workspace, record, archive = _build_in(tmp_path / "workspace", composition=composition)
    bundle_dir = archive.parent / "agent-bundle"
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    provenance = json.loads((bundle_dir / "provenance.json").read_text(encoding="utf-8"))
    compatibility = json.loads(
        (bundle_dir / "compatibility-report.json").read_text(encoding="utf-8")
    )
    compatibility_facts_digest = compatibility["bundle"]["compatibilityFactsDigest"]

    assert manifest["compositionProfileDigest"] == composition.profile_digest
    assert manifest["compositionMode"] == "composed"
    assert record.resolved_digest != ""
    assert json.loads((bundle_dir / "plugin-lock.json").read_text(encoding="utf-8")) == (
        composition.plugin_lock.model_dump(by_alias=True, exclude_none=True, mode="json")
    )
    assert json.loads((bundle_dir / "composition-profile.json").read_text(encoding="utf-8")) == (
        composition.profile.model_dump(by_alias=True, exclude_none=True, mode="json")
    )
    assert provenance["composition"]["profileDigest"] == composition.profile_digest
    assert compatibility_facts_digest.startswith("sha256:")
    assert provenance["compatibility"] == {
        "reportPath": "compatibility-report.json",
        "factsDigest": compatibility_facts_digest,
    }
    assert compatibility == {
        "blockingReasons": [],
        "bundle": {
            "bundleFormat": "agentkit.bundle/v2",
            "compatibilityFactsDigest": compatibility_facts_digest,
            "compositionProfileDigest": composition.profile_digest,
            "pluginLockDigest": composition.plugin_lock_digest,
            "resolvedDigest": record.resolved_digest,
        },
        "capabilities": [
            {
                "definition": "agent.provider/v1",
                "mode": "unique",
                "owner": "io.ksadk.provider",
                "slot": "agent.execution",
                "status": "compatible",
                "version": "1.0.0",
            },
            {
                "definition": "session.event-store/v1",
                "mode": "unique",
                "owner": "io.ksadk.event-store",
                "slot": "session.events",
                "status": "compatible",
                "version": "1.0.0",
            },
        ],
        "format": "agentkit.compatibility-report/v1",
        "host": {
            "kernelApiVersion": "1.0.0",
            "kind": "composition-host",
            "readiness": "notEvaluated",
            "runtimeContract": "agentkit.runtime/v1",
            "status": "compatible",
        },
        "kernelApi": [
            {
                "pluginId": "io.ksadk.event-store",
                "required": ">=1,<2",
                "resolved": "1.0.0",
                "status": "compatible",
            },
            {
                "pluginId": "io.ksadk.provider",
                "required": ">=1,<2",
                "resolved": "1.0.0",
                "status": "compatible",
            },
        ],
        "overallStatus": "compatible",
        "permissions": {
            "allowed": [],
            "plugins": [
                {
                    "pluginId": "io.ksadk.event-store",
                    "required": [],
                    "status": "compatible",
                    "unapproved": [],
                },
                {
                    "pluginId": "io.ksadk.provider",
                    "required": [],
                    "status": "compatible",
                    "unapproved": [],
                },
            ],
            "status": "compatible",
        },
        "protocols": [
            {
                "pluginId": "io.ksadk.event-store",
                "required": ["agentkit.runtime/v1"],
                "status": "compatible",
                "supported": ["agentkit.runtime/v1"],
            },
            {
                "pluginId": "io.ksadk.provider",
                "required": ["agentkit.runtime/v1"],
                "status": "compatible",
                "supported": ["agentkit.runtime/v1"],
            },
        ],
        "provider": {
            "digest": "sha256:" + "1" * 64,
            "hostRuntime": "python",
            "id": "io.ksadk.provider",
            "isolation": "process",
            "ref": "plugin://io.ksadk.provider@1.0.0",
            "source": "builtin",
            "status": "compatible",
            "version": {
                "requested": "1.0.0",
                "resolved": "1.0.0",
                "status": "compatible",
            },
        },
        "scope": "build-time-static",
    }
    assert "composition-profile.json" in {entry["path"] for entry in manifest["files"]}


def test_compatibility_report_records_static_blockers_without_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "must-never-enter-compatibility-report"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    composition = _resolved_composition(
        provider_permissions=("network:model-endpoint",),
        provider_kernel_api=">=2,<3",
        provider_protocols=("agentkit.runtime/v2",),
    )

    _workspace, record, archive = _build_in(
        tmp_path / "workspace",
        composition=composition,
    )
    _, compatible_record, _ = _build_in(
        tmp_path / "compatible",
        composition=_resolved_composition(),
    )
    with zipfile.ZipFile(archive) as bundle:
        report_bytes = bundle.read("compatibility-report.json")
    report = json.loads(report_bytes)

    assert secret.encode() not in report_bytes
    # These compositions have the same Profile and PluginLock digests. The
    # exact compatibility facts must still produce different build identities.
    assert composition.profile_digest == _resolved_composition().profile_digest
    assert composition.plugin_lock_digest == _resolved_composition().plugin_lock_digest
    assert record.resolved_digest != compatible_record.resolved_digest
    assert report["overallStatus"] == "blocked"
    assert report["host"]["status"] == "blocked"
    assert report["provider"]["status"] == "blocked"
    assert report["permissions"]["status"] == "blocked"
    assert report["permissions"]["plugins"][-1]["unapproved"] == ["network:model-endpoint"]
    assert [item["code"] for item in report["blockingReasons"]] == [
        "kernel_api_incompatible",
        "plugin_permission_unapproved",
        "runtime_protocol_incompatible",
    ]


def test_zip_has_fixed_timestamps_and_modes(tmp_path: Path):
    _, _, archive = _build_in(tmp_path / "workspace")

    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == sorted(bundle.namelist())
        for info in bundle.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644


def test_bundle_contains_reference_but_never_secret_value(tmp_path: Path):
    secret = "fixture-secret-value-that-must-not-leak"
    workspace, _, archive = _build_in(tmp_path / "workspace")
    with zipfile.ZipFile(archive) as bundle:
        data = b"\n".join(bundle.read(name) for name in bundle.namelist())

    assert b"env://MODEL_API_KEY" in data
    assert secret.encode() not in data
    for path in workspace.root.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="ignore")


def test_builder_packages_skill_content_into_immutable_bundle(tmp_path: Path):
    root = tmp_path / "workspace"
    skill = root / "capabilities/skills/research"
    skill.mkdir(parents=True)
    (skill / "skill.yaml").write_text(
        "name: research\ninstructionsFile: SKILL.md\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "Always cite primary sources.\n",
        encoding="utf-8",
    )

    _, _, archive = _build_in(
        root,
        skills=[CapabilityRef(name="research", version="1.0.0")],
    )

    with zipfile.ZipFile(archive) as bundle:
        assert (
            bundle.read("capabilities/skills/research/SKILL.md")
            == b"Always cite primary sources.\n"
        )


def test_runtime_bundle_contains_a_detectable_launch_project(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    source = workspace.root / "runtime"
    source.mkdir()
    (source / "agent.py").write_text("graph = object()\n", encoding="utf-8")
    draft = AgentDraft(
        metadata=AgentMetadata(id="studio-graph", name="Studio Graph"),
        spec=AgentSpec(
            instructions=Instructions(system="Be concise.", task="Reply OK."),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            runtime=RuntimeRef(
                type="langgraph",
                project_path="runtime",
                entry_point="agent.py",
                agent_variable="graph",
            ),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )

    record = AgentBundleBuilder(workspace).build(draft)
    archive = workspace.resolve(record.artifact_path or "")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extracted)

    detection = FrameworkDetector(str(extracted / "runtime")).detect()
    assert detection.type == FrameworkType.LANGGRAPH
    assert detection.entry_point == "agent.py"
    assert detection.agent_variable == "graph"
