"""SoulDocument is reviewed source, compiled deterministically, never runtime learning."""
from __future__ import annotations

import zipfile
from pathlib import Path

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
    SoulDocument,
)
from ksadk.studio.service import StudioService
from ksadk.studio.soul import render_soul_markdown, soul_digest
from ksadk.studio.validator import AgentValidator
from ksadk.studio.workspace import Workspace


def _draft(*, system: str = "Explain decisions.") -> AgentDraft:
    return AgentDraft(
        metadata=AgentMetadata(id="soul-agent", name="Soul Agent"),
        spec=AgentSpec(
            instructions=Instructions(system=system, task="Give a concise answer."),
            soul=SoulDocument(
                identity="You are a reliable release assistant.",
                principles=["State evidence before recommendations."],
                boundaries=["Never invent a passing test result."],
                tone="Clear and concise.",
            ),
            model=ModelSpec(
                model="glm-5.1",
                base_url="https://model.example.com/v1",
                credential_ref="env://MODEL_API_KEY",
            ),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
        ),
    )


def test_soul_markdown_and_digest_are_deterministic() -> None:
    soul = _draft().spec.soul
    assert soul is not None
    rendered = render_soul_markdown(soul)

    assert rendered.startswith("# Soul\n\n## Identity\nYou are a reliable release assistant.")
    assert "## Boundaries\n- Never invent a passing test result." in rendered
    assert soul_digest(soul) == soul_digest(SoulDocument.model_validate(soul.model_dump()))


def test_soul_compiles_before_mutable_system_instruction(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    compiled = AgentCompiler(workspace).compile(_draft())

    assert compiled.resolved.soul is not None
    assert compiled.resolved.instructions.system.startswith("# Soul")
    assert compiled.resolved.instructions.system.endswith("Explain decisions.")
    assert compiled.resolved.source_digest
    assert compiled.resolved.resolved_digest


def test_validator_accepts_soul_as_system_source(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    draft = _draft(system="")

    assert AgentValidator().validate(draft).valid
    assert AgentCompiler(workspace).compile(draft).resolved.instructions.system.startswith("# Soul")


def test_bundle_contains_compiled_soul_snapshot(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    record = AgentBundleBuilder(workspace).build(_draft())
    archive = workspace.resolve(record.artifact_path or "", must_exist=True)

    with zipfile.ZipFile(archive) as bundle:
        system = bundle.read("instructions/system.md").decode("utf-8")
        soul = bundle.read("instructions/soul.md").decode("utf-8")
    assert system.startswith("# Soul")
    assert system.endswith("Explain decisions.\n")
    assert soul == render_soul_markdown(_draft().spec.soul)  # type: ignore[arg-type]


def test_agent_detail_reports_reviewed_soul_source_and_digest(tmp_path: Path) -> None:
    studio = StudioService(tmp_path)
    draft = _draft()
    created = studio.drafts.create(
        agent_id=draft.metadata.id,
        name=draft.metadata.name,
        spec=draft.spec,
    )

    projection = studio.agent_detail(created.metadata.id)["soulProjection"]

    assert projection == {
        "present": True,
        "source": "AgentSpec.soul",
        "sourceRevision": created.metadata.revision,
        "schemaVersion": "agentkit.soul/v1",
        "digest": soul_digest(created.spec.soul),  # type: ignore[arg-type]
        "digestAlgorithm": "sha256-canonical-json",
        "compileTarget": "resolved-agent-spec.instructions.system",
        "compileOrder": "before-instructions.system",
    }


def test_agent_detail_reports_runtime_specific_soul_compile_targets() -> None:
    draft = _draft()

    draft.spec.runtime = RuntimeRef(type="codex")
    assert StudioService._soul_projection(draft)["compileTarget"] == (
        "managed-runtime.base_instructions"
    )

    draft.spec.runtime = RuntimeRef(
        type="plugin",
        providerRef="plugin://io.example.provider@1.2.3",
    )
    assert StudioService._soul_projection(draft)["compileTarget"] == (
        "instructions/soul.md"
    )
