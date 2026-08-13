from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.studio.capabilities import LocalCapabilityResolver
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilitiesSpec,
    CapabilityRef,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    SecuritySpec,
    ToolContract,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.validator import AgentValidator
from ksadk.studio.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    return workspace


def _draft(*, tools=None, skills=None, allowed_permissions=None) -> AgentDraft:
    return AgentDraft(
        metadata=AgentMetadata(id="demo-agent", name="Demo Agent"),
        spec=AgentSpec(
            instructions=Instructions(system="You are reliable."),
            model=ModelSpec(
                model="glm-5.1",
                base_url="https://model.example.com/v1",
                credential_ref="env://MODEL_API_KEY",
            ),
            capabilities=CapabilitiesSpec(
                tools=tools or [],
                skills=skills or [],
            ),
            security=SecuritySpec(
                allowed_permissions=allowed_permissions or [],
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )


def test_validator_requires_model_and_system_instruction():
    result = AgentValidator().validate(
        AgentDraft(
            metadata=AgentMetadata(id="demo-agent", name="Demo"),
            spec=AgentSpec(),
        )
    )

    assert not result.valid
    assert {item.code for item in result.diagnostics} == {
        "AGENT_MODEL_REQUIRED",
        "AGENT_SYSTEM_INSTRUCTION_REQUIRED",
    }


def test_validator_rejects_plaintext_secret_and_network_target():
    draft = _draft()
    assert draft.spec.model is not None
    draft.spec.model.credential_ref = "plain-secret-value"
    draft.spec.security.network.allowed_hosts = []

    result = AgentValidator().validate(draft)

    assert not result.valid
    assert {item.code for item in result.diagnostics} == {
        "SECRET_REFERENCE_INVALID",
        "NETWORK_TARGET_DENIED",
    }


def test_validator_checks_tool_schema_permission_and_approval():
    tool = ToolContract(
        name="write-record",
        version="1.0.0",
        input_schema={"type": "not-a-json-schema-type"},
        permissions=["records:write"],
        side_effect="write",
        approval="never",
    )

    result = AgentValidator().validate(_draft(tools=[tool]))

    assert not result.valid
    codes = {item.code for item in result.diagnostics}
    assert "TOOL_SCHEMA_INVALID" in codes
    assert "TOOL_PERMISSION_DENIED" in codes
    assert "TOOL_AUTO_APPROVAL_RISK" in codes
    warning = next(
        item for item in result.diagnostics if item.code == "TOOL_AUTO_APPROVAL_RISK"
    )
    assert warning.severity == "warning"


def test_validator_rejects_mutable_dependency_version():
    result = AgentValidator().validate(
        _draft(skills=[CapabilityRef(name="research", version="latest")])
    )

    assert not result.valid
    assert result.diagnostics[0].code == "CAPABILITY_VERSION_MUTABLE"


def test_local_skill_resolution_is_content_addressed(tmp_path: Path):
    workspace = _workspace(tmp_path)
    skill = workspace.root / "capabilities/skills/research"
    skill.mkdir()
    (skill / "skill.yaml").write_text(
        "name: research\ninstructionsFile: SKILL.md\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text("Use primary sources.\n", encoding="utf-8")
    resolver = LocalCapabilityResolver(workspace)

    first = resolver.resolve_skill(CapabilityRef(name="research", version="1.0.0"))
    (skill / "skill.yaml").write_text("name: changed\n", encoding="utf-8")
    second = resolver.resolve_skill(CapabilityRef(name="research", version="1.0.0"))

    assert first["bundlePath"] == "capabilities/skills/research"
    assert first["instructions"] == "Use primary sources.\n"
    assert first["digest"] != second["digest"]


def test_compiler_normalizes_endpoint_and_is_deterministic(tmp_path: Path):
    workspace = _workspace(tmp_path)
    compiler = AgentCompiler(workspace)
    draft = _draft()

    first = compiler.compile(draft)
    second = compiler.compile(
        AgentDraft.model_validate(
            json.loads(
                json.dumps(
                    draft.model_dump(by_alias=True, mode="json"),
                    sort_keys=False,
                )
            )
        )
    )

    assert first.resolved.model.endpoint_url == (
        "https://model.example.com/v1/chat/completions"
    )
    assert first.resolved.resolved_digest == second.resolved.resolved_digest
    assert first.dependency_lock == second.dependency_lock


def test_compiler_fails_before_resolving_invalid_draft(tmp_path: Path):
    workspace = _workspace(tmp_path)

    with pytest.raises(StudioError) as captured:
        AgentCompiler(workspace).compile(
            AgentDraft(
                metadata=AgentMetadata(id="demo-agent", name="Demo"),
                spec=AgentSpec(instructions=Instructions(system="")),
            )
        )

    assert captured.value.code == "AGENT_SYSTEM_INSTRUCTION_REQUIRED"


def test_validator_rejects_mcp_tool_without_enabled_server():
    tool = ToolContract(
        name="mcp_echo",
        version="1.0.0",
        executor="mcp",
        mcp_server="missing-mcp",
    )

    result = AgentValidator().validate(_draft(tools=[tool]))

    assert not result.valid
    assert any(
        item.code == "CAPABILITY_UNRESOLVED" for item in result.diagnostics
    )
