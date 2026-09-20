from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from ksadk.kernel.execution_grants import execution_grant_run_id
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.kernel.teams_effects import EffectConflict, EffectOutcome, EffectPreparedReceipt
from ksadk.kernel.teams_execution_context import StoreIdentityMismatch
from ksadk.kernel.teams_host_http import create_teams_host_router
from ksadk.kernel.teams_tool_runtime import (
    TeamsEffectTools,
    TrustedEffectAdapter,
    open_effect_journal,
)
from ksadk.plugins.teams.cloud_contracts import digest
from tests.kernel.teams_host_harness import host_harness as host_harness
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres


async def initialize(h):
    journal = await open_effect_journal(h.registry)
    h.host.effect_journal = journal
    facts = {
        "policy": {
            "systemContext": "test",
            "effects": {
                "increment": {
                    "adapterVersion": "counter/v1",
                    "effectClass": "external_idempotent",
                }
            },
        }
    }
    h.context = h.context.model_copy(
        update={
            "context": facts,
            "context_digest": digest(facts),
            "ref": h.context.ref.model_copy(
                update={"capabilitiesDigest": h.host.capabilities_digest}
            ),
        }
    )
    h.preparation = replace(h.preparation, context=h.context)
    await h.prepare()
    await h.submit()
    lease = await h.store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="agent-1",
            session_id="s1",
            activation_id="effects",
            lease_ttl_seconds=60,
        )
    )
    await h.store.claim_next("agent-1", "s1", lease.fencing_token)
    await h.store.save_run_transition(
        RunRecord(
            run_id=execution_grant_run_id(h.context.command),
            agent_instance_id="agent-1",
            session_id="s1",
            state="pending",
        ),
        expected_fence=lease.fencing_token,
    )
    return journal


async def test_actual_effect_wrapper_lost_report_and_recovery_history(host_harness):
    h = host_harness
    journal = await initialize(h)
    sends, calls = [], []

    async def authorize(ctx, name):
        assert ctx == h.context and name == "increment"
        await h.host.kernel.require_execution_grant(ctx.grant)

    async def execute(args, *, effect_key):
        calls.append(effect_key)
        return EffectOutcome(
            phase="completed", evidence_ref="external-proof", result_digest=digest(args)
        )

    async def send(operation, value):
        sends.append(operation)
        if operation == "prepare":
            assert await journal.get_invocation(value)
            return EffectPreparedReceipt(
                effect_key=value.effect_key,
                payload_digest=value.payload_digest,
                request_digest=value.request_digest,
                revision=1,
                phase="prepared",
            )
        raise ConnectionError("lost ACK")

    adapter = TrustedEffectAdapter(
        "increment", {"type": "object"}, "counter/v1", "external_idempotent", execute
    )
    try:
        tools = TeamsEffectTools(
            context=h.context,
            host=h.host,
            journal=journal,
            adapters={"increment": adapter},
            authorize=authorize,
            send=send,
        ).tools()
        first = await tools["increment"].call({}, call_id="original-framework-call")
        assert first["phase"] == "completed" and len(calls) == 1
        assert await tools["increment"].call({}, call_id="original-framework-call") == first
        assert len(calls) == 1
        with pytest.raises(ValueError, match="identity"):
            await tools["increment"].call({})
        with pytest.raises(EffectConflict):
            await tools["increment"].call({"changed": True}, call_id="original-framework-call")
        await h.host.kernel.set_execution_grant_state(
            h.context.grant, "revoked", expected_revision=1, idempotency_key="revoke-effect"
        )
        # A fresh recovery permit reads the real immutable evidence chain after
        # execute permission was withdrawn. It cannot replay the external call.
        app = FastAPI()

        async def resolve_host(**kwargs):
            return h.host

        app.include_router(create_teams_host_router(resolve_host))
        body = {
            "contextRef": h.context.context_ref,
            "expectedIncarnation": h.incarnation,
            "permit": h.permit("observe", recovery=True).model_dump(mode="json"),
            "limit": 2,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://host"
        ) as client:
            first_page = await client.post(
                "/agent-kernel/teams-host/v1/GetExecutionEffects", json=body
            )
            assert first_page.status_code == 200, first_page.text
            first_page = first_page.json()
            assert [x["revision"] for x in first_page["items"]] == [1, 2]
            second = await client.post(
                "/agent-kernel/teams-host/v1/GetExecutionEffects",
                json=body | {"after": first_page["nextCursor"]},
            )
            assert [x["revision"] for x in second.json()["items"]] == [3]
            rejected = await client.post(
                "/agent-kernel/teams-host/v1/GetExecutionEffects",
                json=body | {"expectedIncarnation": "replaced-store"},
            )
            assert rejected.status_code == 409
        with pytest.raises(Exception):
            await tools["increment"].call({}, call_id="fresh-forbidden-call")
        assert len(calls) == 1
    finally:
        await journal.close()


async def test_effect_journal_identity_is_pinned_to_original_kernel(host_harness):
    h = host_harness
    journal = await open_effect_journal(h.registry)
    identity = journal.journal_incarnation
    await journal.close()
    journal = await open_effect_journal(h.registry)
    assert journal.journal_incarnation == identity
    await journal.close()
    with pytest.raises(StoreIdentityMismatch):
        await h.registry.bind_extension_identity(
            "effect-journal", "replacement", expected_incarnation=h.incarnation
        )
    if h.backend == "sqlite":
        journal.path.unlink()
    else:
        async with h.store._connection() as db:
            await db.execute(
                "DELETE FROM teams_effect_pg_meta WHERE namespace=$1", h.store.tenant_id
            )
    with pytest.raises(EffectConflict):
        await open_effect_journal(h.registry)
