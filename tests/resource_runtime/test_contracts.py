import pytest
from pydantic import ValidationError

from ksadk.resource_runtime.contracts import (
    InvocationIdentity,
    ResourceConfig,
    ResourceRef,
    validate_resource_bindings,
)


def config(kind="knowledge-base", **policy):
    return ResourceConfig.model_validate(
        {
            "schemaVersion": 1,
            "binding": {
                "id": "binding-a",
                "connectionRef": "connection-a",
                "resource": {"kind": kind, "id": "resource-a", "region": "region-a"},
            },
            **policy,
        }
    )


def test_defaults_have_same_digest_as_explicit_policy():
    assert (
        config().digest == config(retrieval={"mode": "tool", "topK": 5, "maxChars": 16000}).digest
    )
    assert (
        config("skill-space").digest
        == config("skill-space", selectionMode="pinned", executionMode="outer-agent").digest
    )


@pytest.mark.parametrize(
    "policy",
    [
        {"schemaVersion": 2},
        {"schemaVersion": True},
        {"apiKey": "fake-secret"},
        {"retrieval": {"topK": True}},
        {"retrieval": {"maxChars": 0}},
        {"selectionMode": "discovery"},
    ],
)
def test_invalid_or_foreign_policy_rejected(policy):
    with pytest.raises(ValidationError):
        config(**policy)


def test_memory_cannot_carry_second_policy_source():
    with pytest.raises(ValidationError):
        config("memory-instance", recall={"enabled": True})


def test_skill_requires_exact_hash_and_discovery_cannot_pin():
    skill = {"skillId": "skill-a", "versionId": "version-a", "contentHash": "sha256:" + "a" * 64}
    assert (
        config("skill-space", selectedSkills=[skill]).selected_skills[0].version_id == "version-a"
    )
    for policy in (
        {"selectedSkills": [skill, skill]},
        {"selectedSkills": [skill], "selectionMode": "discovery"},
        {"selectedSkills": [{**skill, "contentHash": "latest"}]},
    ):
        with pytest.raises(ValidationError):
            config("skill-space", **policy)


def test_duplicate_binding_or_kind_rejected():
    a = config()
    with pytest.raises(ValueError, match="IDs"):
        validate_resource_bindings((a, config("memory-instance")))
    b = ResourceConfig.model_validate(
        {**a.model_dump(), "binding": {**a.binding.model_dump(), "id": "binding-b"}}
    )
    with pytest.raises(ValueError, match="each kind"):
        validate_resource_bindings((a, b))


def test_partition_is_stable_across_sessions_and_isolates_users_accounts_and_agents():
    identity = InvocationIdentity(
        tenant_ref="tenant-a",
        resource_principal_ref="account-a",
        actor_ref="actor-a",
        memory_subject_ref="user-a",
        agent_id="agent-a",
        session_ref="session-a",
    )
    resource = ResourceRef(kind="memory-instance", id="memory-a", region="region-a")
    original = identity.memory_partition(resource)
    assert len(original) == 64
    assert original.startswith("mp1-")
    assert original[4:].isalnum()
    assert (
        identity.model_copy(update={"session_ref": "session-b"}).memory_partition(resource)
        == original
    )
    for field in ("tenant_ref", "resource_principal_ref", "memory_subject_ref", "agent_id"):
        assert (
            identity.model_copy(update={field: "different"}).memory_partition(resource) != original
        )
    assert identity.memory_partition(resource.model_copy(update={"id": "memory-b"})) != original
    assert identity.memory_partition(resource.model_copy(update={"region": "region-b"})) != original
