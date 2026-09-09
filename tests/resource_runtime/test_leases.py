import pytest

from ksadk.resource_runtime.leases import LeaseRejected, ResourceLeaseRegistry, ResourceScope


def scope(**changes):
    return ResourceScope.model_validate(
        {
            "profileDigest": "sha256:" + "a" * 64,
            "generationId": "generation-a",
            "activationId": "activation-a",
            "buildDigest": "sha256:" + "b" * 64,
            "bindingSnapshotDigest": "sha256:" + "c" * 64,
            "bindingId": "binding-a",
            "identity": {
                "tenantRef": "tenant-a",
                "resourcePrincipalRef": "account-a",
                "actorRef": "actor-a",
                "memorySubjectRef": "user-a",
                "agentId": "agent-a",
                "sessionRef": "session-a",
            },
            "allowedOperations": ["load_memory"],
            **changes,
        }
    )


def authorize(registry, handle, **changes):
    return registry.authorize(
        handle,
        **{
            "operation": "load_memory",
            "generation_id": "generation-a",
            "activation_id": "activation-a",
            **changes,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "save_memory"},
        {"generation_id": "generation-b"},
        {"activation_id": "activation-b"},
    ],
)
def test_handle_does_not_authorize_other_operation_or_execution(changes):
    registry = ResourceLeaseRegistry()
    lease = registry.issue(scope())
    with pytest.raises(LeaseRejected):
        authorize(registry, lease.handle, **changes)
    assert authorize(registry, lease.handle).identity.memory_subject_ref == "user-a"
    assert lease.handle not in repr(lease)


def test_expiry_and_renewal_rotate_without_widening():
    now = [0]
    registry = ResourceLeaseRegistry(clock=lambda: now[0])
    lease = registry.issue(scope(), lifetime=10)
    now[0] = 9
    renewed = registry.renew(lease.handle, lifetime=10)
    with pytest.raises(LeaseRejected):
        authorize(registry, lease.handle)
    assert authorize(registry, renewed.handle) == lease.scope
    now[0] = 19
    with pytest.raises(LeaseRejected):
        registry.renew(renewed.handle)


def test_revoke_is_scoped_and_handles_do_not_survive_restart():
    registry = ResourceLeaseRegistry()
    a = registry.issue(scope())
    b = registry.issue(scope(activationId="activation-b"))
    registry.revoke_activation("activation-a")
    with pytest.raises(LeaseRejected):
        authorize(registry, a.handle)
    assert authorize(registry, b.handle, activation_id="activation-b") == b.scope
    with pytest.raises(LeaseRejected):
        authorize(ResourceLeaseRegistry(), b.handle, activation_id="activation-b")
    registry.revoke_generation("generation-a")
    with pytest.raises(LeaseRejected):
        authorize(registry, b.handle, activation_id="activation-b")


def test_capacity_reclaims_expired_handles_and_renew_does_not_need_extra_slot():
    now = [0]
    registry = ResourceLeaseRegistry(clock=lambda: now[0], capacity=1)
    lease = registry.issue(scope(), lifetime=10)
    with pytest.raises(RuntimeError, match="CAPACITY"):
        registry.issue(scope())
    registry.renew(lease.handle, lifetime=10)
    now[0] = 10
    assert registry.issue(scope())


@pytest.mark.parametrize("lifetime", [0, 601, True, 1.5])
def test_invalid_lifetime_rejected(lifetime):
    with pytest.raises(ValueError):
        ResourceLeaseRegistry().issue(scope(), lifetime=lifetime)


def test_one_activation_cannot_mix_users_or_builds():
    registry = ResourceLeaseRegistry()
    original = scope()
    registry.issue(original)
    changed_identity = original.identity.model_dump(by_alias=True)
    changed_identity["memorySubjectRef"] = "user-b"
    for changed in (scope(identity=changed_identity), scope(buildDigest="sha256:" + "d" * 64)):
        with pytest.raises(ValueError, match="activation cannot change"):
            registry.issue(changed)
