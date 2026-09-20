from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from ksadk.kernel import ingress
from ksadk.kernel.execution_grants import execution_grant_run_id
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.kernel.teams_host_http import TeamsNativeIngressGate, create_teams_host_router
from ksadk.plugins.teams.cloud_permits import timestamp
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


@pytest.fixture
async def client(host_harness, monkeypatch):
    h = host_harness

    async def resolve_host(**kwargs):
        return h.host

    monkeypatch.setattr(ingress, "_kernel", h.host.kernel)
    monkeypatch.setattr(
        ingress,
        "_teams_ingress_gate",
        TeamsNativeIngressGate(
            resolve_host,
            h.registry.owns_session,
        ),
    )
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "hosted")
    app = FastAPI()
    app.include_router(create_teams_host_router(resolve_host))
    app.include_router(ingress.agent_kernel_router())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def common(h, operation, recovery=False):
    return dict(
        contextRef=h.context.context_ref,
        expectedIncarnation=h.incarnation,
        permit=h.permit(operation, recovery=recovery).model_dump(mode="json"),
    )


async def test_http_prepare_dual_native_permits_and_original_lookup(client, host_harness):
    h = host_harness
    response = await client.post(
        "/agent-kernel/teams-host/v1/PrepareExecution", json=common(h, "prepare")
    )
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["storeIncarnation"] == h.incarnation
    permit = h.native_authority.permit(
        issued_at=timestamp(h.now), expires_at=timestamp(h.now + timedelta(seconds=60))
    )
    body = {
        "command": h.context.command.model_dump(mode="json"),
        "permit": permit.model_dump(mode="json"),
    }
    response = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=body)
    assert response.status_code == 403
    assert (await h.lookup()).status == "missing"
    body["teams"] = common(h, "enqueue")
    response = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=body)
    assert response.status_code == 202, response.text
    accepted = response.json()
    response = await client.post(
        "/agent-kernel/teams-host/v1/LookupExecution",
        json={
            **common(h, "lookup", True),
            "commandId": h.context.ref.commandId,
            "idempotencyKey": h.context.ref.idempotencyKey,
            "payloadDigest": h.context.payload_digest,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["acceptedSeq"] == accepted["accepted_seq"]
    assert response.json()["status"] == "accepted"


async def test_control_lookup_uses_original_inbox_and_cannot_claim_terminal(client, host_harness):
    h = host_harness
    await h.prepare()
    await h.submit()
    run_id = execution_grant_run_id(h.context.command)
    lease = await h.store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="agent-1",
            session_id="s1",
            activation_id="test-control",
            lease_ttl_seconds=60,
        )
    )
    await h.store.claim_next("agent-1", "s1", lease.fencing_token)
    await h.store.save_run_transition(
        RunRecord(
            run_id=run_id,
            agent_instance_id="agent-1",
            session_id="s1",
            state="pending",
            metadata={"command_id": h.context.ref.commandId},
        ),
        expected_fence=lease.fencing_token,
    )
    command = h.context.command.model_copy(
        update={
            "command_id": uuid4(),
            "idempotency_key": "independent-cancel",
            "command_type": "interrupt",
            "payload": {"run_id": run_id, "reason": "user"},
        }
    )

    async def lookup(cmd=command, incarnation=None):
        body = {**common(h, "lookup", True), "command": cmd.model_dump(mode="json")}
        if incarnation:
            body["expectedIncarnation"] = incarnation
        return await client.post("/agent-kernel/teams-host/v1/LookupControlExecution", json=body)

    missing = await lookup()
    assert missing.status_code == 200 and missing.json()["status"] == "missing"
    native = h.native_authority.permit(
        operations=["interrupt"],
        issued_at=timestamp(h.now),
        expires_at=timestamp(h.now + timedelta(seconds=60)),
    )
    accepted = await h.host.submit_native_control(
        command,
        context_ref=h.context.context_ref,
        expected_incarnation=h.incarnation,
        permit=h.permit("cancel", recovery=True),
        native_permit=native,
    )
    response = await lookup()
    assert response.status_code == 200, response.text
    proof = response.json()
    assert proof["status"] == "accepted" and proof["acceptedSeq"] == accepted.acceptedSeq
    assert proof["commandId"] == str(command.command_id) and proof["runId"] == run_id
    assert proof["terminalEvidence"] is None and proof["nativeStatus"] is None
    assert (
        await lookup(
            command.model_copy(update={"payload": {"run_id": run_id, "reason": "changed"}})
        )
    ).status_code == 409
    assert (await lookup(command.model_copy(update={"command_id": uuid4()}))).status_code == 409
    assert (await lookup(h.context.command)).status_code == 409
    assert (await lookup(incarnation="replacement-store")).status_code == 409


async def test_native_permit_refresh_keeps_exact_command_id(client, host_harness):
    h = host_harness
    await h.prepare()
    first = await h.submit()
    native = h.native_authority.permit(
        permit_id="permit-refreshed",
        issued_at=timestamp(h.now),
        expires_at=timestamp(h.now + timedelta(seconds=60)),
    )
    refreshed = h.context.command.model_copy(
        update={
            "authorization_ref": native.permit_id,
            "submitted_at": timestamp(h.now + timedelta(seconds=1)),
            "source": h.context.command.source.model_copy(update={"ref": "new-transport-attempt"}),
        }
    )
    body = {
        "command": refreshed.model_dump(mode="json"),
        "permit": native.model_dump(mode="json"),
        "teams": common(h, "enqueue"),
    }
    response = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "duplicate"
    assert response.json()["accepted_seq"] == first.accepted_seq
    from uuid import uuid4

    body["command"]["command_id"] = str(uuid4())
    response = await client.post(ingress.KERNEL_INGRESS_SUBMIT_PATH, json=body)
    assert response.status_code == 409


async def test_http_invalid_store_and_recovery_cannot_prepare(client, host_harness):
    h = host_harness
    response = await client.post(
        "/agent-kernel/teams-host/v1/PrepareExecution", json=common(h, "lookup", True)
    )
    assert response.status_code == 403
    body = common(h, "prepare") | {"expectedIncarnation": "replaced-store"}
    response = await client.post("/agent-kernel/teams-host/v1/PrepareExecution", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["Code"] == "store_identity_mismatch"


async def test_unenveloped_control_on_reserved_session_cannot_bypass_gate(client, host_harness):
    h = host_harness
    await h.prepare()
    cmd = h.context.command.model_copy(update={"command_type": "interrupt", "payload": {}})
    native = h.native_authority.permit(
        operations=["interrupt"],
        issued_at=timestamp(h.now),
        expires_at=timestamp(h.now + timedelta(seconds=60)),
    )
    response = await client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={
            "command": cmd.model_dump(mode="json"),
            "permit": native.model_dump(mode="json"),
        },
    )
    assert response.status_code == 403
