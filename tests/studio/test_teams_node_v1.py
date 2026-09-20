import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from ksadk.kernel.teams_execution_context import SQLiteTeamsExecutionContextRegistry
from ksadk.plugins.teams.cloud_contracts import NODE_COMMAND_ADAPTER, digest, node_command_digest
from ksadk.plugins.teams.cloud_permits import timestamp
from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.teams_node_v1 import (
    KernelTeamsNodeExecutor,
    TeamsNodeTransport,
    TeamsNodeV1,
    normalized_node_bindings,
)
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


class Transport:
    def __init__(self):
        self.reports = []
        self.fail_ack = False
        self.closed = False
        self.token = None

    def set_access_token(self, value):
        self.token = value

    async def request(self, method, path, body=None):
        if path.endswith("/result"):
            self.reports.append(body)
            if self.fail_ack:
                self.fail_ack = False
                raise TeamsError("teams_transport_unavailable", "lost ack", status=503)
            return {"ackRevision": body["resultRevision"], "disposition": "applied"}
        raise AssertionError(path)

    async def close(self):
        self.closed = True


def credentials(version=1):
    now = datetime.now(timezone.utc)
    return dict(
        authorityId="authority-test",
        nodeId="node-test",
        nodeGeneration=1,
        credentialVersion=version,
        accessToken=f"test-access-{version}",
        refreshSecret=f"test-refresh-{version}",
        accessExpiresAt=timestamp(now + timedelta(minutes=10)),
        refreshExpiresAt=timestamp(now + timedelta(days=1)),
    )


def command(h, operation, payload, *, permit=None):
    recovery = operation in {"lookup", "get_grant"} or (
        operation == "set_grant" and payload["state"] == "revoked"
    )
    permission = {"submit": "enqueue", "set_grant": "revoke" if recovery else "renew_grant"}.get(
        operation, operation
    )
    values = dict(
        nodeCommandId=str(uuid4()),
        operationKey="operation:" + uuid4().hex,
        operation=operation,
        lane="execution" if operation in {"prepare", "submit"} else "control",
        ref=h.context.ref.model_dump(mode="json"),
        commandDigest=digest("placeholder"),
        issuedAt=timestamp(h.now),
        claimLeaseUntil=timestamp(h.now + timedelta(seconds=30)),
        authorization={
            "permitKind": "recovery" if recovery else "execute",
            "permit": (permit or h.permit(permission, recovery=recovery)).model_dump_json(),
        },
        payload=payload,
    )
    parsed = NODE_COMMAND_ADAPTER.validate_python(values)
    return parsed.model_copy(update={"commandDigest": node_command_digest(parsed)})


def prepare(h):
    return command(
        h,
        "prepare",
        {"contextRef": h.context.context_ref, "contextDigest": h.context.context_digest},
    )


def submit(h):
    return command(
        h,
        "submit",
        {
            "command": h.context.command.model_dump(mode="json"),
            "payloadDigest": h.context.payload_digest,
        },
    )


async def make_node(h, state_dir, *, transport=None, native_provider=None):
    async def resolve(command):
        return h.host

    async def native(command):
        return h.native_authority.permit(
            issued_at=timestamp(h.now), expires_at=timestamp(h.now + timedelta(seconds=60))
        )

    executor = KernelTeamsNodeExecutor(resolve, native_permit_provider=native_provider or native)
    node = TeamsNodeV1(
        AsyncMock(),
        executor,
        state_dir=state_dir,
        name="test",
        bindings=[],
        node_transport=transport or Transport(),
    )
    node._install_credentials(credentials())
    node._lease_deadline = time.monotonic() + 45
    return node


async def process(node, h, order):
    return await node.process_command(order, server_time=h.now, request_started=time.monotonic())


async def test_prepare_and_submit_are_separate_and_ack_loss_replays_exact_report(
    host_harness, tmp_path
):
    h = host_harness
    node = await make_node(h, tmp_path / "node")
    try:
        prepared = await process(node, h, prepare(h))
        assert prepared.phase == "prepared"
        assert (
            await h.store.load_by_idempotency(h.context.ref.sessionId, h.context.ref.idempotencyKey)
            is None
        )
        await node.flush_outbox()
        order = submit(h)
        submitted = await process(node, h, order)
        assert submitted.phase == "submitted" and submitted.receipt.status == "accepted"
        node.transport.fail_ack = True
        with pytest.raises(TeamsError):
            await node.flush_outbox()
        original = node.transport.reports[-1]
        # Even a fresh claim must flush the pending report before another execution step.
        called = h.host.submit_execution
        h.host.submit_execution = AsyncMock(side_effect=AssertionError("must not resubmit"))
        await process(node, h, order)
        await node.flush_outbox()
        assert node.transport.reports[-1] == original
        h.host.submit_execution.assert_not_awaited()
        h.host.submit_execution = called
        assert (
            node.db.execute("SELECT count(*) FROM node_v1_outbox WHERE acked=0").fetchone()[0] == 0
        )
    finally:
        await node.close()


async def test_kill_after_native_admission_recovers_original_without_new_submit(
    host_harness, tmp_path
):
    h = host_harness
    directory = tmp_path / "node"
    node = await make_node(h, directory)
    order = submit(h)
    await process(node, h, prepare(h))
    await node.flush_outbox()
    original = h.host.submit_execution

    async def admitted_then_killed(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError()

    h.host.submit_execution = admitted_then_killed
    with pytest.raises(asyncio.CancelledError):
        await process(node, h, order)
    assert (
        node.db.execute(
            "SELECT state FROM node_v1_operations WHERE id=?", (order.nodeCommandId,)
        ).fetchone()[0]
        == "invoking"
    )
    await node.close()
    replacement = None
    if h.backend == "sqlite":
        from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

        await h.store.close()
        replacement = SQLiteAgentKernelStore(h.store.db_path, h.events)
        await replacement.ensure_schema()
        h.host.store = h.host.kernel._store = replacement
        h.host.registry = SQLiteTeamsExecutionContextRegistry(replacement)
        assert await h.host.registry.initialize() == h.incarnation
    h.host.submit_execution = AsyncMock(side_effect=AssertionError("must query existing inbox"))
    restored = await make_node(h, directory)
    try:
        result = await process(restored, h, order)
        assert result.receipt.status == "accepted"
        assert result.receipt.commandId == h.context.ref.commandId
        h.host.submit_execution.assert_not_awaited()
    finally:
        h.host.submit_execution = original
        await restored.close()
        if replacement:
            await replacement.close()


async def test_old_generation_and_changed_original_command_are_rejected(host_harness, tmp_path):
    h = host_harness
    node = await make_node(h, tmp_path / "node")
    try:
        order = prepare(h)
        await process(node, h, order)
        await node.flush_outbox()
        changed = order.model_dump(mode="json")
        changed["payload"]["contextDigest"] = digest("different")
        parsed = NODE_COMMAND_ADAPTER.validate_python(changed)
        changed = parsed.model_copy(update={"commandDigest": node_command_digest(parsed)})
        with pytest.raises(TeamsError, match="不能改变"):
            await process(node, h, changed)
        node.identity["nodeGeneration"] = 2
        with pytest.raises(TeamsError) as failure:
            await process(node, h, order)
        assert failure.value.code == "node_generation_mismatch"
    finally:
        await node.close()


async def test_revoked_grant_never_admits_node_submit(host_harness, tmp_path):
    h = host_harness
    node = await make_node(h, tmp_path / "node")
    try:
        await process(node, h, prepare(h))
        await node.flush_outbox()
        revoke = command(
            h,
            "set_grant",
            dict(
                grantId=h.context.grant.grant_id,
                expectedRevision=1,
                state="revoked",
                attemptEpoch=1,
                expiresAt=h.preparation.grant_window.expires_at,
                controlId="stop-node",
            ),
        )
        result = await process(node, h, revoke)
        assert result.phase == "control_applied"
        assert result.operationResult.mutationReceipt.snapshot.grant.state == "revoked"
        await node.flush_outbox()
        result = await process(node, h, submit(h))
        assert result.phase == "uncertain"
        assert (
            await h.store.load_by_idempotency(h.context.ref.sessionId, h.context.ref.idempotencyKey)
            is None
        )
    finally:
        await node.close()


async def test_node_transport_and_refresh_never_send_owner_credentials():
    seen = []

    async def endpoint(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = TeamsNodeTransport(
        "https://teams.example.test/teams", transport=httpx.MockTransport(endpoint)
    )
    transport.set_access_token("test-node-token")
    try:
        await transport.request("POST", "/commands/claim", {"lane": "control"})
        await transport.refresh("test-node", "test-refresh", "stable-rotation")
        assert seen[0].headers["X-Teams-Node-Token"] == "test-node-token"
        assert "authorization" not in seen[0].headers
        assert "x-teams-node-token" not in seen[1].headers
        assert "authorization" not in seen[1].headers
        assert json.loads(seen[1].content)["rotationId"] == "stable-rotation"
    finally:
        await transport.close()


async def test_rotation_ack_loss_reuses_original_secret_and_rotation_id(tmp_path):
    transport = Transport()
    requests = []

    async def refresh(node_id, secret, rotation_id):
        requests.append((node_id, secret, rotation_id))
        if len(requests) == 1:
            raise TeamsError("teams_transport_unavailable", "lost ack", status=503)
        return credentials(2)

    transport.refresh = refresh
    directory = tmp_path / "rotation"
    first = TeamsNodeV1(
        AsyncMock(),
        AsyncMock(),
        state_dir=directory,
        name="test",
        bindings=[],
        node_transport=transport,
    )
    first._install_credentials(credentials())
    with pytest.raises(TeamsError):
        await first.refresh_credentials()
    await first.close()
    restored = TeamsNodeV1(
        AsyncMock(),
        AsyncMock(),
        state_dir=directory,
        name="test",
        bindings=[],
        node_transport=transport,
    )
    try:
        await restored.register()
        assert requests[0] == requests[1]
        assert restored.identity["credentialVersion"] == 2
        assert "rotation" not in restored.identity
        assert transport.token == "test-access-2"
    finally:
        await restored.close()


def test_catalog_normalization_includes_server_default_capability_flags():
    advertisement = dict(
        localBindingRef="local-build:one",
        agentId="agent",
        buildId="one",
        providerRef="harness",
        name="example",
        bundleDigest=digest("bundle"),
        contractDigest=digest("contract"),
        capabilitiesDigest=digest("capabilities"),
    )
    normalized = normalized_node_bindings([advertisement])[0]
    assert len(normalized["capabilities"]) == 11
    assert not any(normalized["capabilities"].values())


@pytest.mark.local_process_heavy
def test_actual_sigkill_after_kernel_commit_recovers_same_command(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    arguments = [sys.executable, "-m", "tests.studio.node_v1_crash_fixture", str(tmp_path)]
    crashed = subprocess.run(
        arguments + ["kill"], cwd=root, capture_output=True, text=True, timeout=30
    )
    assert crashed.returncode == -9, crashed.stderr
    recovered = subprocess.run(
        arguments + ["recover"], cwd=root, capture_output=True, text=True, timeout=30
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout) == {
        "status": "accepted",
        "sameCommand": True,
        "inboxRows": 1,
    }


async def test_control_and_heartbeat_progress_while_execution_lane_is_blocked(
    host_harness, tmp_path
):
    h = host_harness
    node = await make_node(h, tmp_path / "node")
    await process(node, h, prepare(h))
    await node.flush_outbox()
    entered, release = asyncio.Event(), asyncio.Event()
    original = node.executor.execute

    async def blocked_execution(order, **kwargs):
        if order.operation == "submit":
            entered.set()
            await release.wait()
        return await original(order, **kwargs)

    node.executor.execute = blocked_execution
    execution = submit(h)
    control = command(h, "get_grant", {"grantId": h.context.grant.grant_id})
    previous = node.transport.request
    heartbeat_seen = []

    async def endpoint(method, path, body=None):
        if path == "/commands/claim":
            order = execution if body["lane"] == "execution" else control
            return {"commands": [order.model_dump(mode="json")], "serverTime": timestamp(h.now)}
        if path.endswith("/heartbeat"):
            heartbeat_seen.append(body)
            return {
                "serverTime": timestamp(h.now),
                "leaseUntil": timestamp(h.now + timedelta(seconds=45)),
                "actions": [],
            }
        return await previous(method, path, body)

    node.transport.request = endpoint
    pending = asyncio.create_task(node.poll_once("execution"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.wait_for(
            asyncio.gather(node.poll_once("control"), node.heartbeat_once()), timeout=5
        )
        assert heartbeat_seen
        assert (
            node.db.execute(
                "SELECT state FROM node_v1_operations WHERE id=?", (control.nodeCommandId,)
            ).fetchone()[0]
            == "control_applied"
        )
        assert not pending.done()
    finally:
        release.set()
        await pending
        await node.close()


async def test_credentials_cannot_turn_replaced_journal_into_a_fresh_node(tmp_path):
    from ksadk.kernel.teams_execution_context import StoreIdentityMismatch

    directory = tmp_path / "node"
    node = TeamsNodeV1(
        AsyncMock(),
        AsyncMock(),
        state_dir=directory,
        name="test",
        bindings=[],
        node_transport=Transport(),
    )
    node._install_credentials(credentials())
    await node.close()
    (directory / "node-v1.sqlite").unlink()
    with pytest.raises(StoreIdentityMismatch, match="original node journal"):
        TeamsNodeV1(
            AsyncMock(),
            AsyncMock(),
            state_dir=directory,
            name="test",
            bindings=[],
            node_transport=Transport(),
        )


async def test_restart_requires_new_heartbeat_before_any_claimed_execution(host_harness, tmp_path):
    h = host_harness
    directory = tmp_path / "node"
    node = await make_node(h, directory)
    await node.close()
    restored = TeamsNodeV1(
        AsyncMock(),
        AsyncMock(),
        state_dir=directory,
        name="test",
        bindings=[],
        node_transport=Transport(),
    )
    try:
        with pytest.raises(TeamsError) as error:
            await process(restored, h, prepare(h))
        assert error.value.code == "node_lease_expired"
        restored.executor.execute.assert_not_awaited()
    finally:
        await restored.close()


async def test_cancel_uses_original_native_run_and_ack_is_not_terminal(host_harness, tmp_path):
    from ksadk.kernel.contracts import AgentControlCommand
    from ksadk.kernel.execution_grants import execution_grant_run_id
    from ksadk.kernel.store import ActivationLeaseRequest, RunRecord

    h = host_harness
    node = await make_node(h, tmp_path / "node")
    try:
        await process(node, h, prepare(h))
        await node.flush_outbox()
        await process(node, h, submit(h))
        await node.flush_outbox()
        run_id = execution_grant_run_id(h.context.command)
        lease = await h.store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=h.context.grant.agent_instance_id,
                session_id=h.context.ref.sessionId,
                activation_id="node-cancel-test",
                lease_ttl_seconds=60,
            )
        )
        await h.store.claim_next(
            h.context.grant.agent_instance_id, h.context.ref.sessionId, lease.fencing_token
        )
        await h.store.save_run_transition(
            RunRecord(
                run_id=run_id,
                agent_instance_id=h.context.grant.agent_instance_id,
                session_id=h.context.ref.sessionId,
                state="pending",
                metadata={"command_id": h.context.ref.commandId},
            ),
            expected_fence=lease.fencing_token,
        )
        control_id = str(uuid4())
        raw = dict(
            nodeCommandId=str(uuid4()),
            operationKey="cancel-operation",
            operation="cancel",
            lane="control",
            ref=h.context.ref.model_dump(mode="json") | {"nativeRunId": run_id},
            commandDigest=digest("placeholder"),
            issuedAt=timestamp(h.now),
            claimLeaseUntil=timestamp(h.now + timedelta(seconds=30)),
            authorization={
                "permitKind": "recovery",
                "permit": h.permit(
                    "cancel", recovery=True, allowedOperations=["cancel", "lookup"]
                ).model_dump_json(),
            },
            payload={
                "controlCommandId": control_id,
                "controlIdempotencyKey": "cancel-key",
                "targetRunId": run_id,
                "reason": "user stop",
            },
        )
        parsed = NODE_COMMAND_ADAPTER.validate_python(raw)
        cancel = parsed.model_copy(update={"commandDigest": node_command_digest(parsed)})
        native = AgentControlCommand.model_validate(
            h.context.command.model_dump(mode="json")
            | {
                "command_id": control_id,
                "idempotency_key": "cancel-key",
                "command_type": "interrupt",
                "payload": {"run_id": run_id, "reason": "user stop"},
            }
        )
        native_permit = h.native_authority.permit(
            operations=("interrupt",),
            issued_at=timestamp(h.now),
            expires_at=timestamp(h.now + timedelta(seconds=60)),
        )
        node.executor.native_control_provider = AsyncMock(return_value=(native, native_permit))
        report = await process(node, h, cancel)
        assert report.phase == "submitted"
        assert report.receipt.commandId == control_id
        assert report.receipt.runId == run_id
        assert report.receipt.terminalEvidence is None
        assert (await h.store.load_run(run_id)).state == "pending"
        assert (
            await h.store.load_by_idempotency(h.context.ref.sessionId, "cancel-key")
        ).command.payload["run_id"] == run_id
    finally:
        await node.close()


async def test_revoked_credentials_stop_admission_and_preserve_journal(host_harness, tmp_path):
    h = host_harness
    node = await make_node(h, tmp_path / "node")
    try:
        await process(node, h, prepare(h))
        node._failure(
            TeamsError("credential_reissue_required", "explicit owner recovery", status=409)
        )
        assert node._credential_blocked
        assert (
            node.db.execute("SELECT count(*) FROM node_v1_outbox WHERE acked=0").fetchone()[0] == 1
        )
        with pytest.raises(TeamsError) as error:
            await process(node, h, submit(h))
        assert error.value.code == "node_lease_expired"
        assert (
            await h.store.load_by_idempotency(h.context.ref.sessionId, h.context.ref.idempotencyKey)
            is None
        )
    finally:
        await node.close()
