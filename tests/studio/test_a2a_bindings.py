from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.contracts import (
    AgentBindings,
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RemoteAgentBinding,
    SecuritySpec,
)
from ksadk.studio.repository import AgentDraftRepository
from ksadk.studio.workspace import Workspace

SPACE = "a2a-space-00000000000040008000000000000011"
AGENT = "a2a-agent-00000000000040008000000000000012"
VERSION = "a2a-version-00000000000040008000000000000013"


def binding(**changes) -> RemoteAgentBinding:
    values = {
        "space_id": SPACE,
        "target_agent_id": AGENT,
        "expected_version_id": VERSION,
        "local_name": "finance",
        "exposure": "native",
        "policy_ref": "policy://a2a/strict",
    }
    values.update(changes)
    return RemoteAgentBinding.freeze(**values)


def test_remote_binding_freeze_is_deterministic_and_tamper_evident() -> None:
    first = binding()
    second = binding()

    assert first == second
    assert first.binding_id.startswith("a2a-binding-")
    assert first.digest.startswith("sha256:")
    assert RemoteAgentBinding.model_validate_json(first.model_dump_json()) == first

    payload = first.model_dump()
    payload["expected_version_id"] = "a2a-version-00000000000040008000000000000014"
    with pytest.raises(ValidationError, match="A2A_BINDING_DIGEST_MISMATCH"):
        RemoteAgentBinding.model_validate(payload)


def test_agent_bindings_default_legacy_revision_and_reject_duplicate_remote_names() -> None:
    assert AgentBindings.model_validate({}).remote_agents == []

    with pytest.raises(ValidationError, match="remoteAgents localName"):
        AgentBindings(remote_agents=[binding(), binding(target_agent_id=AGENT)])


def test_remote_binding_rejects_noncanonical_ids_and_unsafe_policy_refs() -> None:
    with pytest.raises(ValidationError):
        binding(space_id="as-legacy")
    with pytest.raises(ValidationError):
        binding(policy_ref="https://control.example.com/policy")


def test_bundle_contains_only_frozen_remote_binding_contract(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    remote = binding()
    draft = AgentDraft(
        metadata=AgentMetadata(id="remote-demo", name="Remote Demo"),
        spec=AgentSpec(
            instructions=Instructions(system="Delegate finance requests."),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            bindings=AgentBindings(remote_agents=[remote]),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )

    record = AgentBundleBuilder(workspace).build(draft)
    bundle_root = workspace.resolve(record.artifact_path or "").parent / "agent-bundle"
    resolved = json.loads((bundle_root / "resolved-agent-spec.json").read_text(encoding="utf-8"))
    lock = json.loads((bundle_root / "agentkit.lock").read_text(encoding="utf-8"))

    expected = remote.model_dump(by_alias=True, mode="json")
    assert set(expected) == {
        "bindingId",
        "spaceId",
        "targetAgentId",
        "expectedVersionId",
        "localName",
        "exposure",
        "policyRef",
        "digest",
    }
    assert resolved["remoteAgents"] == [expected]
    assert lock["remoteAgents"] == [expected]
    remote_payload = json.dumps(expected, sort_keys=True).lower()
    assert "credential" not in remote_payload
    assert "permit" not in remote_payload
    assert "endpoint" not in remote_payload
    assert "http://" not in remote_payload
    assert "https://" not in remote_payload


def test_remote_bindings_persist_through_draft_repository_round_trip(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = AgentDraftRepository(workspace)
    remote = binding()

    created = repository.create(
        agent_id="remote-persisted",
        name="Remote Persisted",
        spec=AgentSpec(bindings=AgentBindings(remote_agents=[remote])),
    )
    loaded = repository.get(created.metadata.id)

    assert loaded == created
    assert loaded.spec.bindings.remote_agents == [remote]


def test_legacy_persisted_revision_without_remote_agents_defaults_to_empty_list(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = AgentDraftRepository(workspace)
    created = repository.create(
        agent_id="legacy-persisted",
        name="Legacy Persisted",
        spec=AgentSpec(),
    )
    path = workspace.resolve("agents/legacy-persisted/agent.yaml")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["spec"]["bindings"].pop("remoteAgents")
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    loaded = repository.get(created.metadata.id)

    assert loaded.metadata.revision == created.metadata.revision
    assert loaded.spec.bindings.remote_agents == []


def test_frozen_binding_cannot_be_mutated_or_include_route_credentials():
    frozen = binding()
    with pytest.raises(ValidationError, match="frozen_instance"):
        frozen.local_name = "different"
    for field in ("endpoint", "credential", "callPermit"):
        with pytest.raises(ValidationError):
            RemoteAgentBinding.model_validate({**frozen.model_dump(), field: "forbidden"})


def test_one_target_cannot_have_two_exposures():
    with pytest.raises(ValidationError, match="A2A_EXPOSURE_CONFLICT"):
        AgentBindings(remote_agents=[binding(), binding(local_name="second", exposure="tool")])


def test_unknown_policy_revision_remains_readable_but_cannot_compile(tmp_path):
    from ksadk.studio.compiler import AgentCompiler
    from ksadk.studio.errors import StudioError
    old = binding(policy_ref="policy://missing-policy")
    assert RemoteAgentBinding.model_validate_json(old.model_dump_json()) == old
    draft = AgentDraft(metadata=AgentMetadata(id="policy-check", name="Policy check"),
        spec=AgentSpec(bindings=AgentBindings(remote_agents=[old])))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    with pytest.raises(StudioError, match="A2A_POLICY_REF_UNSUPPORTED"):
        AgentCompiler(workspace).compile(draft)
