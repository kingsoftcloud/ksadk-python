from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilitiesSpec,
    CapabilityRef,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.detection import FrameworkDetector, FrameworkType
from ksadk.studio.workspace import Workspace


def _build_in(
    root: Path,
    *,
    credential_ref: str = "env://MODEL_API_KEY",
    skills: list[CapabilityRef] | None = None,
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
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    record = AgentBundleBuilder(workspace).build(draft)
    archive = workspace.resolve(record.artifact_path or "")
    return workspace, record, archive


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
    assert (bundle_dir / "sbom.spdx.json").is_file()
    assert (bundle_dir / "provenance.json").is_file()
    assert (bundle_dir / "checksums.txt").is_file()

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundleDigest"] == record.bundle_digest
    assert manifest["bundleFormat"] == "agentkit.bundle/v2"
    assert manifest["runtimeContract"] == "agentkit.runtime/v1"
    assert manifest["pluginLockDigest"].startswith("sha256:")
    assert json.loads((bundle_dir / "plugin-lock.json").read_text(encoding="utf-8")) == {
        "lockFormat": "agentkit.plugin-lock/v1",
        "plugins": [],
    }
    assert {item["path"] for item in manifest["files"]} >= {
        "resolved-agent-spec.json",
        "agentkit.lock",
        "checksums.txt",
        "instructions/system.md",
    }
    assert workspace.relative(archive) == record.artifact_path


def test_builder_is_byte_deterministic_across_workspaces(tmp_path: Path):
    _, first_record, first_archive = _build_in(tmp_path / "first")
    _, second_record, second_archive = _build_in(tmp_path / "second")

    assert first_record.id == second_record.id
    assert first_record.resolved_digest == second_record.resolved_digest
    assert first_record.bundle_digest == second_record.bundle_digest
    assert (
        hashlib.sha256(first_archive.read_bytes()).digest()
        == hashlib.sha256(second_archive.read_bytes()).digest()
    )


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
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
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
