"""Actual RuntimeApp lifespan + PG inbox + ManagedHarness, no user Agent calls."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter, managed_harness_capabilities
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.kernel import ingress
from ksadk.kernel.bootstrap import clear_agent_kernel_runtime
from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    runtime_capability_matrix_digest,
)
from ksadk.kernel.execution_grants import ExecutionGrantSpec, execution_grant_run_id
from ksadk.kernel.teams_execution_context import PreparedTeamsContext
from ksadk.kernel.teams_runtime_bootstrap import CloudTeamsHostConfig, CloudTeamsRuntimeHost
from ksadk.plugins.teams.cloud_contracts import CloudTarget, TeamsExecutionRef, digest
from ksadk.plugins.teams.cloud_permits import (
    BindingProbePermit,
    TeamsCallbackPermit,
    TeamsExecutionPermit,
    sign_permit,
    timestamp,
)
from ksadk.runtime import StartRequest
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from tests.kernel.control_harness import command
from tests.kernel.teams_host_harness import HostHarness, PermitAuthority, Signer
from tests.kernel.teams_host_harness import temporary_postgres as temporary_postgres

BASE = "/agent-kernel/teams-host/v1"


class Server:
    def __init__(self):
        self.signer, self.native = Signer(), PermitAuthority()
        self.resolve_calls, self.invoke_calls = [], []
        self.context = self.incarnation = None
        self.revision = 1
        self.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.callback_changes = {}
        self.fail_resolve = False

    def callback(self):
        c, now = self.context, datetime.now(timezone.utc)
        unsigned = TeamsCallbackPermit(
            issuer="host-test",
            kid=self.signer.key_id,
            authorityId=c.ref.authorityId,
            permitKind="execute",
            groupId=c.ref.groupId,
            teamRunId=c.ref.teamRunId,
            memberId=c.ref.memberId,
            commandId=c.ref.commandId,
            sessionId=c.ref.sessionId,
            agentInstanceId=c.grant.agent_instance_id,
            bundleDigest=c.ref.bundleDigest,
            policyDigest=c.policy_digest,
            attemptEpoch=1,
            leaderEpoch=1,
            grantRevision=self.revision,
            allowedOperations=["policy", "invoke"],
            issuedAt=timestamp(now),
            expiresAt=timestamp(self.expires),
            nonce=str(uuid4()),
        )
        return sign_permit(unsigned.model_copy(update=self.callback_changes), self.signer)

    async def request(self, req):
        from ksadk.kernel.authorization import b64url_encode

        if req.url.path == "/teams/jwks":
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kty": "OKP",
                            "crv": "Ed25519",
                            "kid": self.signer.key_id,
                            "x": b64url_encode(self.signer.private.public_key().public_bytes_raw()),
                        }
                    ]
                },
            )
        body = json.loads(req.content)
        if req.url.path.endswith("/resolve"):
            self.resolve_calls.append(body)
            if self.fail_resolve:
                return httpx.Response(503)
            permit = TeamsExecutionPermit.model_validate_json(
                req.headers["X-Teams-Execution-Permit"]
            )
            assert permit.permitKind == "execute"
            assert "authorization" not in req.headers
            assert permit.commandId == self.context.ref.commandId
            assert set(body) <= {"contextRef", "operationId"}
            return httpx.Response(
                200,
                json={
                    "context": self.context.model_dump(mode="json"),
                    "storeIncarnation": self.incarnation,
                    "serverTime": timestamp(datetime.now(timezone.utc)),
                    "expiresAt": timestamp(self.expires),
                    "callbackPermit": self.callback().model_dump(mode="json"),
                },
            )
        if req.url.path.endswith("/invoke"):
            callback = TeamsCallbackPermit.model_validate_json(
                req.headers["X-Teams-Callback-Permit"]
            )
            assert callback.commandId == self.context.ref.commandId
            assert callback.grantRevision == self.revision
            assert "authorization" not in req.headers
            self.invoke_calls.append(body)
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(req.url.path)


class Reasoner:
    def __init__(self):
        self.calls = 0
        self.after_model = None

    async def complete(self, *, prompt, tools, **kwargs):
        self.calls += 1
        assert "Server-owned Cloud policy" in prompt
        if self.calls == 1:
            if self.after_model is not None:
                await self.after_model()
            return HarnessReasoningTurn(
                tool_calls=(HarnessToolCall("cloud-call", "team_message", {"content": "hello"}),)
            )
        return HarnessReasoningTurn(final_text="canonical cloud result")


@pytest.fixture
async def cloud(tmp_path, monkeypatch, temporary_postgres, request):
    import asyncpg

    server, reasoner = Server(), Reasoner()
    admin = await asyncpg.connect(temporary_postgres.get_uri())
    name = "runtime_host_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    settings = {
        "AGENT_KERNEL_ENABLED": "1",
        "AGENT_KERNEL_AUTHORITY_MODE": "hosted",
        "AGENT_KERNEL_STORE_DRIVER": "postgres",
        "AGENT_KERNEL_STORE_DSN": temporary_postgres.get_uri(name),
        "AGENT_CONTROL_JWKS_URL": "https://fixture.invalid/native-jwks",
        "AGENT_CONTROL_PERMIT_ISSUER": "host-test",
        "AGENT_INSTANCE_ID": "agent-1",
        "POD_UID": "test-pod",
        "KSADK_TENANT_ID": "tenant-1",
        "AGENT_BUNDLE_DIGEST": digest("bundle"),
        "AGENT_KERNEL_CONTRACT_DIGEST": AGENT_KERNEL_V1_AGGREGATE_DIGEST,
        "AGENT_KERNEL_CAPABILITY_DIGEST": runtime_capability_matrix_digest(
            managed_harness_capabilities(durable=True, execution_policy=True)
        ),
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingress, "_remote_jwks_source", lambda _: server.native.jwks())
    clear_agent_kernel_runtime()
    ingress.clear_agent_kernel()
    target = CloudTarget(
        kind="cloud_agent",
        agentId="cloud-agent",
        versionId="v1",
        runtimeId="runtime-1",
        agentInstanceId="agent-1",
    )
    config = CloudTeamsHostConfig(
        "https://fixture.invalid/teams", "host-test", target, "harness", tmp_path
    )
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(server.request))

    def provider():
        return ManagedHarnessRuntimeAdapter(
            HarnessSpec(
                agent_revision_ref="agent-revision://fixture@1",
                model=ModelBinding(profile_ref="model-profile://fake@1"),
                prompt=PromptSpec(instructions="fixture"),
            ),
            reasoner=reasoner,
            durable=True,
            workspace_root=tmp_path,
        )

    build = None
    if getattr(request, "param", False):
        from tests.kernel.loaded_build_harness import loaded_build

        build = loaded_build(tmp_path / "loaded-build")

    def loaded_factory(runtime):
        assert runtime.config.agent_instance_id == config.target.agentInstanceId
        return build.provider

    def make():
        factory = CloudTeamsRuntimeHost(
            config,
            client=transport_client,
            loaded_build_factory=loaded_factory if build else None,
        )
        app = create_runtime_app(
            RuntimeAppConfig(
                route_groups=set(), kernel_adapter_provider=provider, teams_host_factory=factory
            ),
            lambda app, *_: app.include_router(ingress.agent_kernel_router()),
        )
        return app, factory

    app, factory = make()
    async with app.router.lifespan_context(app):
        descriptor = await factory.descriptor()
        native = command(
            idempotency_key="cloud-original-key",
            payload={
                "content": "go",
                "teams_context_ref": "context-test",
                "execution_policy_ref": "policy-test",
                "execution_grant_id": "grant-test",
                "execution_grant_attempt_epoch": 1,
            },
        )
        ref = TeamsExecutionRef(
            authorityId="authority-test",
            groupId="group-test",
            teamRunId="team-run-test",
            memberId="member-test",
            runMemberId="run-member-test",
            taskId="task-test",
            attemptId="attempt-test",
            deliveryId="delivery-test",
            bindingRef="binding-test",
            providerRef="harness",
            sessionId=native.session_id,
            commandId=str(native.command_id),
            idempotencyKey=native.idempotency_key,
            schedulerEpoch=1,
            leaderEpoch=1,
            dispatchEpoch=1,
            attemptEpoch=1,
            target=target,
            bundleDigest=descriptor["bundleDigest"],
            contractDigest=descriptor["contractDigest"],
            capabilitiesDigest=descriptor["capabilitiesDigest"],
        )
        facts = {
            "policy": {
                "systemContext": "Server-owned Cloud policy",
                "limits": {"max_model_calls": 2},
            }
        }
        context = PreparedTeamsContext(
            context_ref="context-test",
            context_digest=digest(facts),
            policy_ref="policy-test",
            policy_digest=digest("policy"),
            owner_subject="owner-test",
            ref=ref,
            command=native,
            context=facts,
            grant=ExecutionGrantSpec(
                grant_id="grant-test",
                tenant_id="tenant-1",
                agent_instance_id="agent-1",
                session_id="s1",
                owner_ref="host-test",
                attempt_epoch=1,
            ),
        )
        server.context, server.incarnation = context, descriptor["storeIncarnation"]
        server.expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        h = HostHarness(
            "postgres",
            None,
            context,
            factory.registry,
            factory.runtime.kernel_store,
            factory.runtime.session_events,
            factory.runtime.config.session_service,
            server.signer,
            server.native,
            server.incarnation,
            datetime.now(timezone.utc),
            None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://runtime"
        ) as client:
            yield SimpleNamespace(
                app=app,
                factory=factory,
                server=server,
                reasoner=reasoner,
                client=client,
                h=h,
                make=make,
                loaded_build=build,
            )
    await transport_client.aclose()
    await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    await admin.close()


def body(c, operation, *, recovery=False, revision=1):
    c.h.now = datetime.now(timezone.utc)
    return {
        "contextRef": c.h.context.context_ref,
        "expectedIncarnation": c.h.incarnation,
        "permit": c.h.permit(operation, recovery=recovery, revision=revision).model_dump(
            mode="json"
        ),
    }


async def prepare(c):
    response = await c.client.post(BASE + "/PrepareExecution", json=body(c, "prepare"))
    assert response.status_code == 200, response.text
    return response.json()


def request(c):
    ctx = c.h.context
    return StartRequest(
        session_id=ctx.ref.sessionId,
        user_id=ctx.owner_subject,
        input="go",
        metadata={
            "teams_context_ref": ctx.context_ref,
            "execution_policy_ref": ctx.policy_ref,
            "command_id": ctx.ref.commandId,
            "run_id": execution_grant_run_id(ctx.command),
        },
    )


async def test_runtime_lifespan_pg_native_ingress_tool_and_terminal_result(cloud):
    c = cloud
    assert c.factory.runtime.worker_running
    assert (await c.factory.descriptor())["capabilities"]["teamsReady"] is True
    assert (await c.factory.descriptor())["capabilities"]["sharedAcrossHosts"] is True
    await prepare(c)
    original = c.h.context.command
    native = c.server.native.permit(
        issued_at=timestamp(datetime.now(timezone.utc)),
        expires_at=timestamp(datetime.now(timezone.utc) + timedelta(seconds=60)),
    )
    response = await c.client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={
            "command": original.model_dump(mode="json"),
            "permit": native.model_dump(mode="json"),
            "teams": body(c, "enqueue"),
        },
    )
    assert response.status_code == 202, response.text
    for _ in range(200):
        response = await c.client.post(
            BASE + "/GetExecutionResult", json=body(c, "get_result", recovery=True)
        )
        assert response.status_code == 200, response.text
        if response.json().get("status") == "succeeded":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("actual Cloud worker did not complete original run")
    result = response.json()
    assert result["candidate"]["result"] == "canonical cloud result"
    assert result["receipt"]["runId"] == execution_grant_run_id(original)
    assert c.reasoner.calls == 2
    assert (
        len(c.server.resolve_calls) == 1
    )  # model/tool uses durable context, not a request ContextVar
    assert c.server.invoke_calls == [
        {
            "contextRef": "context-test",
            "commandId": c.h.context.ref.commandId,
            "operation": "team_message",
            "arguments": {"content": "hello"},
            "callId": "cloud-call",
        }
    ]
    callback = await c.factory.registry.get_callback(
        "context-test", 1, expected_incarnation=c.h.incarnation
    )
    assert callback is not None
    assert callback.grantRevision == 1


async def test_restart_keeps_original_run_and_never_reanchors_prepare(cloud):
    c = cloud
    await test_runtime_lifespan_pg_native_ingress_tool_and_terminal_result(c)
    await c.factory.runtime.close()
    clear_agent_kernel_runtime()
    ingress.clear_agent_kernel()
    c.server.fail_resolve = True
    app, restarted = c.make()
    async with app.router.lifespan_context(app):
        assert await restarted.registry.incarnation() == c.h.incarnation
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://runtime"
        ) as client:
            response = await client.post(
                BASE + "/LookupExecution",
                json=body(c, "lookup", recovery=True)
                | {
                    "commandId": c.h.context.ref.commandId,
                    "idempotencyKey": c.h.context.ref.idempotencyKey,
                    "payloadDigest": c.h.context.payload_digest,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["runId"] == execution_grant_run_id(c.h.context.command)
            # PG uses its own persisted authoritative deadline. Restart may
            # still use the remaining original grant; it must never renew it.
            before = await restarted.runtime.kernel.get_execution_grant(c.h.context.grant)
            await restarted.resolve("policy-test", request=request(c))
            after = await restarted.runtime.kernel.get_execution_grant(c.h.context.grant)
            assert after.grant.expires_at == before.grant.expires_at
            assert after.grant.revision == before.grant.revision == 1
            assert c.reasoner.calls == 2
            assert len(c.server.resolve_calls) == 1


async def test_renewal_uses_original_operation_and_new_callback_revision(cloud):
    c = cloud
    await prepare(c)
    c.server.revision = 2
    await asyncio.sleep(0.2)
    c.server.expires = datetime.now(timezone.utc) + timedelta(seconds=29.99)
    response = await c.client.post(
        BASE + "/SetExecutionGrant",
        json=body(c, "renew_grant")
        | {
            "state": "active",
            "expectedRevision": 1,
            "renewalId": "original-renewal",
            "expiresAt": timestamp(c.server.expires),
        },
    )
    assert response.status_code == 200, response.text
    assert c.server.resolve_calls[-1] == {
        "contextRef": "context-test",
        "operationId": "original-renewal",
    }
    assert response.json()["mutationReceipt"]["snapshot"]["grant"]["revision"] == 2
    policy = await c.factory.resolve("policy-test", request=request(c))
    assert policy.system_context == "Server-owned Cloud policy"
    stored = await c.factory.registry.get_callback(
        "context-test", 2, expected_incarnation=c.h.incarnation
    )
    assert stored.grantRevision == 2


@pytest.mark.parametrize(
    "changes",
    [{"grantRevision": 2}, {"memberId": "other-member"}, {"allowedOperations": ["invoke"]}],
)
async def test_callback_scope_or_missing_policy_permission_fails_closed(cloud, changes):
    c = cloud
    c.server.callback_changes = changes
    response = await c.client.post(BASE + "/PrepareExecution", json=body(c, "prepare"))
    assert response.status_code == 403, response.text
    assert c.reasoner.calls == 0
    assert (
        await c.factory.registry.get_callback(
            "context-test", 1, expected_incarnation=c.h.incarnation
        )
        is None
    )


async def test_recovery_cannot_load_context_and_wrong_store_is_never_missing(cloud):
    c = cloud
    response = await c.client.post(
        BASE + "/PrepareExecution", json=body(c, "lookup", recovery=True)
    )
    assert response.status_code == 409
    assert response.json()["error"]["Code"] == "store_identity_mismatch"
    assert c.server.resolve_calls == []
    c.server.incarnation = "new-empty-store"
    response = await c.client.post(BASE + "/PrepareExecution", json=body(c, "prepare"))
    assert response.status_code == 409
    assert response.json()["error"]["Code"] == "store_identity_mismatch"
    assert c.reasoner.calls == 0


async def test_real_probe_and_revocation_blocks_per_model_boundary(cloud):
    from ksadk.kernel.execution_grants import ExecutionGrantBlocked

    c = cloud
    desc = await c.factory.descriptor()
    now = datetime.now(timezone.utc)
    probe = sign_permit(
        BindingProbePermit(
            issuer="host-test",
            kid=c.server.signer.key_id,
            authorityId="authority-test",
            probeId=str(uuid4()),
            bindingRef="binding-test",
            target=c.factory.config.target,
            expectedDigests={
                "bundle": desc["bundleDigest"],
                "contract": desc["contractDigest"],
                "capabilities": desc["capabilitiesDigest"],
            },
            allowedOperations=["describe"],
            issuedAt=timestamp(now),
            expiresAt=timestamp(now + timedelta(seconds=30)),
            nonce=str(uuid4()),
        ),
        c.server.signer,
    )
    response = await c.client.post(
        BASE + "/DescribeBinding", json={"permit": probe.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    await prepare(c)
    await c.factory.resolve("policy-test", request=request(c))
    await c.factory.runtime.kernel.set_execution_grant_state(
        c.h.context.grant, "revoked", expected_revision=1, idempotency_key="stop"
    )
    with pytest.raises(ExecutionGrantBlocked):
        await c.factory.resolve("policy-test", request=request(c))
    assert not c.server.invoke_calls


def test_missing_configuration_has_no_host_and_partial_config_rejected(monkeypatch):
    for key in ("SERVER_URL", "TARGET", "PROVIDER_REF"):
        monkeypatch.delenv("KSADK_TEAMS_RUNTIME_" + key, raising=False)
    assert CloudTeamsRuntimeHost.from_env() is None
    app = create_runtime_app(RuntimeAppConfig(route_groups=set()))
    assert not hasattr(app.state, "teams_host")
    monkeypatch.setenv("KSADK_TEAMS_RUNTIME_SERVER_URL", "https://fixture.invalid/teams")
    with pytest.raises(RuntimeError, match="incomplete"):
        CloudTeamsRuntimeHost.from_env()


async def test_callback_expiry_uses_pg_time_not_runtime_wall_clock(cloud, monkeypatch):
    from ksadk.kernel import teams_runtime_bootstrap as module
    from ksadk.plugins.teams.cloud_permits import PermitError

    c = cloud
    c.server.callback_changes = {
        "expiresAt": timestamp(datetime.now(timezone.utc) + timedelta(seconds=0.6))
    }
    await prepare(c)

    class WrongClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2000, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", WrongClock)
    await c.factory.resolve("policy-test", request=request(c))
    await asyncio.sleep(0.65)
    with pytest.raises(PermitError, match="not currently valid"):
        await c.factory.resolve("policy-test", request=request(c))
    assert len(c.server.resolve_calls) == 1
    assert not c.server.invoke_calls


async def test_actual_model_to_tool_boundary_observes_revocation(cloud):
    c = cloud
    await prepare(c)

    async def revoke():
        await c.factory.runtime.kernel.set_execution_grant_state(
            c.h.context.grant,
            "revoked",
            expected_revision=1,
            idempotency_key="revoke-after-model",
        )

    c.reasoner.after_model = revoke
    native = c.server.native.permit(
        issued_at=timestamp(datetime.now(timezone.utc)),
        expires_at=timestamp(datetime.now(timezone.utc) + timedelta(seconds=60)),
    )
    response = await c.client.post(
        ingress.KERNEL_INGRESS_SUBMIT_PATH,
        json={
            "command": c.h.context.command.model_dump(mode="json"),
            "permit": native.model_dump(mode="json"),
            "teams": body(c, "enqueue"),
        },
    )
    assert response.status_code == 202, response.text
    for _ in range(150):
        response = await c.client.post(
            BASE + "/GetExecutionResult", json=body(c, "get_result", recovery=True)
        )
        assert response.status_code == 200, response.text
        if response.json().get("status") in {"failed", "cancelled", "interrupted"}:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("revoked execution did not terminate")
    assert c.reasoner.calls == 1
    assert not c.server.invoke_calls


def test_unsupported_runtime_cannot_claim_teams_policy_support(tmp_path):
    from tests.kernel.control_harness import FakeAdapter

    factory = CloudTeamsRuntimeHost(
        CloudTeamsHostConfig(
            "https://fixture.invalid/teams",
            "host-test",
            CloudTarget(
                kind="cloud_agent", agentId="a", versionId="v", runtimeId="r", agentInstanceId="i"
            ),
            "harness",
            tmp_path,
        ),
        client=object(),
    )
    with pytest.raises(RuntimeError, match="ManagedHarness"):
        factory.wrap_adapter_provider(lambda: FakeAdapter())()
    assert not factory.policy_mounted


@pytest.mark.parametrize("cloud", [True], indirect=True)
async def test_real_cloud_factory_loaded_archive_changes_invalidate_original_binding(cloud):
    from ksadk.kernel.teams_execution_context import ContextConflict

    c = cloud
    descriptor = await c.factory.descriptor()
    assert descriptor["capabilities"]["loadedBuild"]["codeArtifactDigest"] == (
        c.loaded_build.archive.code_digest
    )
    assert digest(descriptor["capabilities"]) == descriptor["capabilitiesDigest"]
    c.factory._host("authority-test", "binding-test")._binding_check(c.h.context)
    c.loaded_build.configuration.behavior_config["temperature"] = 0.8
    changed = await c.factory.descriptor()
    assert "loadedBuild" not in changed["capabilities"]
    assert changed["capabilities"]["teamsReady"] is True
    assert changed["capabilitiesDigest"] != descriptor["capabilitiesDigest"]
    with pytest.raises(ContextConflict, match="capability digest mismatch"):
        c.factory._host("authority-test", "binding-test")._binding_check(c.h.context)
