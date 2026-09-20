from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from ksadk.events.canonical import ItemCompleted, OutputRef, RunCompleted, SourceRef, UsageReported
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.execution_grants import ExecutionGrantBlocked, execution_grant_run_id
from ksadk.kernel.execution_host_ingress import TrustedGrantWindow
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.kernel.teams_execution_context import ContextConflict, StoreIdentityMismatch
from ksadk.plugins.teams.cloud_contracts import digest
from ksadk.plugins.teams.cloud_permits import (
    BindingProbePermit,
    PermitError,
    sign_permit,
    timestamp,
)
from ksadk.runtime import StartRequest
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


async def test_describe_reports_actual_policy_and_storage_readiness(host_harness):
    h = host_harness
    probe = BindingProbePermit(
        issuer="host-test",
        kid=h.signer.key_id,
        authorityId=h.context.ref.authorityId,
        probeId=str(uuid4()),
        bindingRef=h.context.ref.bindingRef,
        target=h.context.ref.target,
        expectedDigests={
            "bundle": h.context.ref.bundleDigest,
            "contract": h.context.ref.contractDigest,
            "capabilities": h.host.capabilities_digest,
        },
        allowedOperations=["describe"],
        issuedAt=timestamp(h.now),
        expiresAt=timestamp(h.now + timedelta(seconds=10)),
        nonce=str(uuid4()),
    )
    value = await h.host.describe_binding(permit=sign_permit(probe, h.signer))
    assert value["capabilities"]["executionPolicy"] is True
    assert value["storeIncarnation"] == h.incarnation
    assert value["capabilities"]["sharedAcrossHosts"] == (h.backend == "postgres")
    h.host.policy_enforcement_ready = False
    assert h.host.capability_snapshot()["teamsReady"] is False
    with pytest.raises(ContextConflict, match="not ready"):
        await h.prepare()
    with pytest.raises((PermitError, ValueError)):
        await h.host.describe_binding(permit=h.permit("prepare"))


async def test_prepare_lookup_lost_ack_native_submit_and_original_identity(host_harness):
    h = host_harness
    prepared = await h.prepare()
    assert prepared.snapshot.grant.revision == 1
    assert (await h.prepare()) == prepared
    assert (await h.lookup()).status == "missing"
    accepted = await h.submit()
    assert accepted.status == "accepted"
    lookup = await h.lookup()
    assert lookup.status == "accepted" and lookup.acceptedSeq == accepted.accepted_seq
    assert lookup.runId == execution_grant_run_id(h.context.command)
    assert (await h.submit()).status == "duplicate"
    with pytest.raises(ContextConflict, match="original command"):
        await h.lookup(idempotency_key="fresh-key")
    with pytest.raises(StoreIdentityMismatch):
        await h.lookup(expected_incarnation=str(uuid4()))
    with pytest.raises(StoreIdentityMismatch):
        await h.lookup(context_ref="unknown-original")


async def test_native_permit_is_independently_verified(host_harness):
    h = host_harness
    await h.prepare()
    bad = h.native_authority.permit(
        issued_at=timestamp(h.now), expires_at=timestamp(h.now + timedelta(seconds=60))
    )
    bad = bad.model_copy(update={"signature": "bad"})
    receipt = await h.host.submit_execution(
        h.context.command,
        expected_incarnation=h.incarnation,
        permit=h.permit("enqueue"),
        native_permit=bad,
    )
    assert receipt.status == "rejected" and receipt.error.code == "invalid_permit"
    assert (await h.lookup()).status == "missing"


async def test_forged_scope_and_changed_command_fail_before_submit(host_harness):
    h = host_harness
    await h.prepare()
    for change in (
        {"policyDigest": digest("other")},
        {"attemptEpoch": 2},
        {"subjectRef": "another-owner"},
        {"agentInstanceId": "another-instance"},
        {"dispatchEpoch": 2},
        {"leaderEpoch": 2},
    ):
        with pytest.raises(PermitError):
            await h.host.validate_enqueue(
                h.context.command,
                expected_incarnation=h.incarnation,
                permit=h.permit("enqueue", **change),
            )
    changed = h.context.command.model_copy(
        update={"payload": h.context.command.payload | {"content": "mutated"}}
    )
    with pytest.raises(ContextConflict):
        await h.host.validate_enqueue(
            changed, expected_incarnation=h.incarnation, permit=h.permit("enqueue")
        )
    assert (await h.lookup()).status == "missing"


async def test_recovery_cannot_enqueue_or_admit_and_can_read_revoked_original(host_harness):
    h = host_harness
    await h.prepare()
    await h.submit()
    with pytest.raises(PermitError):
        await h.host.validate_enqueue(
            h.context.command,
            expected_incarnation=h.incarnation,
            permit=h.permit("lookup", recovery=True),
        )
    revoked = await h.host.set_execution_grant(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("revoke", recovery=True),
        state="revoked",
        expected_revision=1,
        control_id="stop-1",
    )
    assert revoked.mutationReceipt.snapshot.grant.state == "revoked"
    assert (
        await h.lookup(permit=h.permit("lookup", recovery=True, revision=1))
    ).status == "accepted"
    with pytest.raises((PermitError, ExecutionGrantBlocked)):
        await h.host.validate_enqueue(
            h.context.command,
            expected_incarnation=h.incarnation,
            permit=h.permit("enqueue", revision=2),
        )
    with pytest.raises(PermitError):
        await h.host.set_execution_admission(
            context_ref=h.context.context_ref,
            expected_incarnation=h.incarnation,
            permit=h.permit("get_grant", recovery=True, revision=2),
            allowed=True,
            expected_revision=1,
            control_id="wrong-kind",
        )


async def test_admission_revision_is_independent_and_renewal_receipts_are_durable(host_harness):
    h = host_harness
    await h.prepare()
    paused = await h.host.set_execution_admission(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("set_admission"),
        allowed=False,
        expected_revision=1,
        control_id="pause-1",
    )
    grant = paused.mutationReceipt.snapshot.grant
    assert grant.state == "active" and grant.revision == 1 and grant.admissionRevision == 2
    assert not grant.admissionAllowed
    # Pausing new claims does not prevent policy resolution or renewal.
    await h.host.kernel.require_execution_grant(h.context.grant)
    renewed = await h.host.renew_execution_grant(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("renew_grant"),
        expected_revision=1,
        renewal_id="renew-1",
        trusted_window=replace(
            h.preparation.grant_window, expires_at=timestamp(h.now + timedelta(seconds=29))
        ),
    )
    got = await h.host.get_execution_grant(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("get_grant", recovery=True, revision=1),
        renewal_id="renew-1",
    )
    assert got.mutationReceipt == renewed.mutationReceipt
    assert got.current.grant.admissionRevision == 2
    assert got.current.grant.revision == 2


async def test_policy_resolver_checks_persisted_command_and_live_grant(host_harness):
    h = host_harness
    await h.prepare()
    request = StartRequest(
        input="task",
        user_id="owner-test",
        session_id=h.context.ref.sessionId,
        metadata={
            "command_id": h.context.ref.commandId,
            "run_id": execution_grant_run_id(h.context.command),
            "execution_policy_ref": h.context.policy_ref,
            "teams_context_ref": h.context.context_ref,
        },
    )
    assert (
        await h.host.resolve(h.context.policy_ref, request=request)
    ).system_context == "trusted policy"
    wrong = request.model_copy(update={"metadata": request.metadata | {"command_id": str(uuid4())}})
    with pytest.raises(ContextConflict):
        await h.host.resolve(h.context.policy_ref, request=wrong)
    await h.host.set_execution_grant(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("revoke", recovery=True),
        state="revoked",
        expected_revision=1,
        control_id="stop-policy",
    )
    with pytest.raises(ExecutionGrantBlocked):
        await h.host.resolve(h.context.policy_ref, request=request)


async def test_canonical_result_ignores_child_and_commentary_and_includes_usage(host_harness):
    h = host_harness
    await h.prepare()
    await h.submit()
    run_id = execution_grant_run_id(h.context.command)
    lease = await h.store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="agent-1",
            session_id="s1",
            activation_id="host-result",
            lease_ttl_seconds=60,
        )
    )
    await h.store.claim_next("agent-1", "s1", lease.fencing_token)
    await h.store.save_run_transition(
        RunRecord(run_id=run_id, agent_instance_id="agent-1", session_id="s1", state="pending"),
        expected_fence=lease.fencing_token,
    )

    async def result():
        return await h.host.get_execution_result(
            context_ref=h.context.context_ref,
            expected_incarnation=h.incarnation,
            permit=h.permit("get_result", recovery=True),
        )

    assert (await result())["status"] == "pending"
    view = RuntimeEventStore(h.events, session_id="s1")
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )

    def event_args(**changes):
        return (
            dict(
                schema_version=2,
                event_id=str(uuid4()),
                seq=0,
                timestamp=h.now.timestamp(),
                run_id=run_id,
                scope_id=run_id,
                source=SourceRef(framework="ksadk"),
            )
            | changes
        )

    def message(text, item_id, **changes):
        return ItemCompleted(
            **event_args(**changes),
            item_id=item_id,
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text=text),)),
        )

    await view.append(message("commentary only", "commentary"), guard=guard)
    await view.append(
        RunCompleted(
            **event_args(source=SourceRef(framework="ksadk", metadata={"parent_run_id": "parent"})),
            status="completed",
            output_refs=(),
        ),
        guard=guard,
    )
    assert (await result())["status"] == "pending"
    await view.append(message("canonical final", "final"), guard=guard)
    await view.append(
        UsageReported(**event_args(), input_tokens=3, output_tokens=4, total_tokens=7), guard=guard
    )
    terminal = await view.append(
        RunCompleted(
            **event_args(),
            status="completed",
            output_refs=(OutputRef(scope_id=run_id, item_id="final"),),
        ),
        guard=guard,
    )
    output = await result()
    assert output["status"] == "succeeded"
    assert output["candidate"] == {"result": "canonical final", "artifacts": []}
    assert output["usage"] == {"totalTokens": 7}
    assert output["terminalEvidence"].terminalSeq == terminal.seq
    assert output["terminalEvidence"].resultDigest == digest(output["candidate"])
    assert await result() == output
    batch = await h.host.observe_execution(
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("observe", recovery=True),
        after_seq=0,
        limit=2,
    )
    upper, seen = batch.snapshotUpperSeq, list(batch.items)
    assert batch.hasMore
    # New events must not move a previously frozen snapshot watermark.
    await view.append(message("arrived after the snapshot", "later"), guard=guard)
    while batch.hasMore:
        batch = await h.host.observe_execution(
            context_ref=h.context.context_ref,
            expected_incarnation=h.incarnation,
            permit=h.permit("observe", recovery=True),
            after_seq=batch.nextSeq,
            limit=2,
            snapshot_upper_seq=upper,
        )
        seen.extend(batch.items)
    assert batch.nextSeq == upper
    assert max(item.seq for item in seen) == terminal.seq
    assert len({item.eventId for item in seen}) == len(seen)
    assert all(item.runId == run_id for item in seen)


@pytest.mark.parametrize("elapsed", [0, 2, 8])
def test_trusted_ttl_subtracts_response_delay_and_margin(elapsed):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    window = TrustedGrantWindow(timestamp(now + timedelta(seconds=10)), now, 100, 0.5)
    assert window.remaining(lambda: 100 + elapsed) == pytest.approx(9.5 - elapsed, abs=0.002)
    with pytest.raises(PermitError):
        window.remaining(lambda: 111)
    with pytest.raises(PermitError):
        window.remaining(lambda: 99)


async def test_prepare_checks_actual_session_owner(host_harness):
    h = host_harness
    original = h.host.session_ensurer

    async def wrong_owner(context):
        session = await original(context)
        return replace(session, user_id="unrelated-user")

    h.host.session_ensurer = wrong_owner
    with pytest.raises(ContextConflict, match="session owner"):
        await h.prepare()
    assert await h.host.kernel.get_execution_grant(h.context.grant) is None


async def test_prepare_replay_cannot_reanchor_restarted_local_grant(host_harness):
    h = host_harness
    await h.prepare()
    if h.backend == "sqlite":
        from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
        from ksadk.kernel.teams_execution_context import SQLiteTeamsExecutionContextRegistry

        await h.store.close()
        replacement = SQLiteAgentKernelStore(h.store.db_path, h.events)
        await replacement.ensure_schema()
        registry = SQLiteTeamsExecutionContextRegistry(replacement)
        assert await registry.initialize() == h.incarnation
        h.store = h.host.store = h.host.kernel._store = replacement
        h.registry = h.host.registry = registry
        try:
            with pytest.raises(ExecutionGrantBlocked, match="clock_unverified"):
                await h.prepare()
            # The recovery path can still determine the exact inbox result.
            assert (await h.lookup()).status == "missing"
            renewed = await h.host.renew_execution_grant(
                context_ref=h.context.context_ref,
                expected_incarnation=h.incarnation,
                permit=h.permit("renew_grant"),
                expected_revision=1,
                renewal_id="fresh-after-restart",
                trusted_window=replace(
                    h.preparation.grant_window, expires_at=timestamp(h.now + timedelta(seconds=29))
                ),
            )
            assert renewed.mutationReceipt.snapshot.grant.revision == 2
            await h.host.kernel.require_execution_grant(h.context.grant)
        finally:
            await replacement.close()
    else:
        # PG uses database clock, not a process-local anchor.
        assert (await h.prepare()).snapshot.grant.revision == 1


async def test_execute_expiry_does_not_block_recovery(host_harness):
    h = host_harness
    await h.prepare()
    await h.submit()
    if h.backend == "sqlite":
        expiry, _ = h.store._grant_deadlines[h.context.grant.grant_id]
        h.store._grant_deadlines[h.context.grant.grant_id] = (expiry, 0)
    else:
        async with h.store._connection() as connection:
            await connection.execute(
                "UPDATE kernel_execution_grants "
                "SET expires_at=clock_timestamp()-interval '1 second' "
                "WHERE grant_id=$1",
                h.context.grant.grant_id,
            )
    with pytest.raises((PermitError, ExecutionGrantBlocked)):
        await h.host.validate_enqueue(
            h.context.command, expected_incarnation=h.incarnation, permit=h.permit("enqueue")
        )
    assert (await h.lookup()).status == "accepted"


async def test_old_deployment_bundle_cannot_report_durable_missing(host_harness):
    h = host_harness
    await h.prepare()
    h.host.binding = replace(h.host.binding, bundle_digest=digest("upgraded deployment"))
    with pytest.raises(ContextConflict, match="original runtime"):
        await h.lookup()


async def test_prepare_accounts_for_session_io_before_grant_anchor(host_harness):
    h = host_harness
    started = h.preparation.grant_window.request_started_monotonic
    current = [started]
    h.host.monotonic = lambda: current[0]
    original = h.host.session_ensurer

    async def slow_session(context):
        value = await original(context)
        current[0] += 16  # 20s TTL minus 5s margin is already exhausted.
        return value

    h.host.session_ensurer = slow_session
    with pytest.raises(PermitError, match="expired in transit"):
        await h.prepare()
    assert await h.host.kernel.get_execution_grant(h.context.grant) is None


async def test_material_execution_requires_real_trusted_preparation(host_harness):
    h = host_harness
    from ksadk.kernel.teams_execution_context import PreparedTeamsContext

    material_context = PreparedTeamsContext.model_validate(
        h.context.model_dump(mode="json")
        | {"material_manifest_ref": "manifest-test", "material_manifest_digest": digest("manifest")}
    )
    h.context = material_context
    h.preparation = replace(h.preparation, context=material_context)
    with pytest.raises(ContextConflict, match="material verification"):
        await h.prepare()
    assert await h.host.kernel.get_execution_grant(h.context.grant) is None


async def test_native_permit_owner_and_session_are_independent_fences(host_harness):
    h = host_harness
    await h.prepare()
    for changes in (
        {"subject_ref": "other-owner"},
        {"session_id": None},
        {"session_id": "other-session"},
    ):
        permit = h.native_authority.permit(
            issued_at=timestamp(h.now),
            expires_at=timestamp(h.now + timedelta(seconds=30)),
            **changes,
        )
        with pytest.raises(PermitError, match="native and Teams"):
            await h.host.submit_execution(
                h.context.command,
                expected_incarnation=h.incarnation,
                permit=h.permit("enqueue"),
                native_permit=permit,
            )
    assert (await h.lookup()).status == "missing"


async def test_native_nonce_is_durable_with_original_kernel_database(host_harness):
    h = host_harness
    assert await h.registry.register("nonce-original", "command-one", "key-one")
    assert await h.registry.register("nonce-original", "command-one", "key-one")
    assert not await h.registry.register("nonce-original", "command-two", "key-one")
    replacement = type(h.registry)(h.store)
    assert await replacement.initialize() == h.incarnation
    assert not await replacement.register("nonce-original", "command-two", "key-two")
    assert await replacement.register("nonce-original", "command-one", "key-one")
