import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.kernel.execution_grants import ExecutionGrantSpec
from ksadk.kernel.teams_execution_context import PreparedTeamsContext
from ksadk.plugins.teams.cloud_contracts import (
    NodeProbeCommand,
    TeamsExecutionRef,
    digest,
    node_probe_digest,
)
from ksadk.plugins.teams.cloud_permits import BindingProbePermit, sign_permit, timestamp
from ksadk.runtime import RuntimeLaunchContext, StartRequest
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.teams_node_factory import create_studio_teams_node
from tests.kernel.control_harness import command as native_command
from tests.kernel.teams_host_harness import HostHarness, PermitAuthority, Signer
from tests.studio.test_teams_node_v1 import command, credentials, prepare, process, submit


class Server:
    base_url = "https://fixture.invalid/teams"

    def __init__(self):
        self.signer, self.native_authority = Signer(), PermitAuthority()
        self.context = None
        self.incarnation = None
        self.key_requests = 0
        self.context_requests = []
        self.invocations = []
        self.reports = []
        self.version = 0
        self.lose_ack = False

    def set_access_token(self, value):
        self.token = value

    async def refresh(self, *args):
        self.version += 1
        return credentials(self.version)

    async def request(self, method, path, body=None):
        if path == "/nodes/register":
            self.version += 1
            return credentials(self.version)
        if path.endswith("/configuration"):
            return {
                "authorityId": "authority-test",
                "tenantId": "tenant-1",
                "protocolVersion": "teams-node/v1",
            }
        if path == "/jwks":
            self.key_requests += 1
            from ksadk.kernel.authorization import b64url_encode

            return {
                "keys": [
                    {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "kid": self.signer.key_id,
                        "x": b64url_encode(self.signer.private.public_key().public_bytes_raw()),
                    },
                    {
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "kid": self.native_authority.key_id,
                        "x": next(
                            iter(
                                (
                                    await self.native_authority.jwks().fetch_verification_keys()
                                ).values()
                            )
                        ),
                    },
                ]
            }
        if path.endswith("/catalog"):
            return {"acceptedRevision": body["revision"], "catalogDigest": digest(body["bindings"])}
        if path.endswith("/contexts/resolve"):
            self.context_requests.append(body)
            return {
                "context": self.context.model_dump(mode="json"),
                "storeIncarnation": self.incarnation,
                "serverTime": timestamp(self.now),
                "expiresAt": timestamp(self.now + timedelta(seconds=30)),
            }
        if path.endswith("/contexts/invoke"):
            self.invocations.append(body)
            return {"accepted": True}
        if path.endswith("/result"):
            self.reports.append(body)
            if self.lose_ack:
                self.lose_ack = False
                raise ConnectionError("test ack lost")
            return {"ackRevision": body["resultRevision"], "disposition": "applied"}
        raise AssertionError(path)

    async def close(self):
        pass


class Reasoner:
    def __init__(self):
        self.calls = []

    async def complete(self, *, prompt, messages, tools, **kwargs):
        self.calls.append((prompt, tools))
        assert "trusted policy from Server" in prompt
        assert "team_message" in [tool.name for tool in tools]
        if len(self.calls) == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall("factory-call", "team_message", {"content": "progress"}),
                )
            )
        return HarnessReasoningTurn(final_text="canonical production factory result")


@pytest.fixture
async def setup(tmp_path, request):
    server, reasoner = Server(), Reasoner()
    sessions = LocalSessionService(db_path=tmp_path / "session.sqlite")
    spec = StudioRunSpec(
        RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
        build_id="build-test",
        agent_id="agent-test",
        model="fixture-model",
        manifest_sha256=digest("bundle"),
        request_config={"provider_ref": "harness"},
    )
    studio = SimpleNamespace(
        session_service=sessions,
        runtime_executor=None,
        plugin_runs=SimpleNamespace(execution_policy_resolver=None),
        configuration=SimpleNamespace(
            environment=lambda: {"KSADK_TEAMS_PERMIT_ISSUER": "host-test"}
        ),
        resolve_run_spec=lambda _: spec,
        drafts=SimpleNamespace(list=lambda **kwargs: []),
        builds=SimpleNamespace(
            list=lambda: [
                SimpleNamespace(
                    id="build-test",
                    agent_id="agent-test",
                    status="SUCCEEDED",
                    artifact_path="fixture",
                    runtime_type="harness",
                    runtime_lock={},
                    created_at=datetime.now(timezone.utc),
                    bundle_digest=digest("bundle"),
                    resolved_digest=digest("bundle"),
                )
            ]
        ),
    )

    def provider(_):
        return lambda: ManagedHarnessRuntimeAdapter(
            HarnessSpec(
                agent_revision_ref="agent-revision://fixture@1",
                model=ModelBinding(profile_ref="model-profile://fake@1"),
                prompt=PromptSpec(instructions="fixture"),
            ),
            reasoner=reasoner,
            durable=True,
            workspace_root=tmp_path,
            execution_policy_resolver=studio.plugin_runs.execution_policy_resolver,
        )

    studio._scheduler_adapter_provider = provider
    build = None
    if getattr(request, "param", False):
        from tests.kernel.loaded_build_harness import loaded_build

        build = loaded_build(tmp_path / "loaded-build")

    def loaded_factory(runtime, actual_spec, build_id):
        assert actual_spec is spec and build_id == spec.build_id
        assert runtime.config.agent_instance_id
        return build.provider

    async def make():
        node = await create_studio_teams_node(
            studio=studio,
            owner_client=server,
            node_transport=server,
            state_dir=tmp_path / "node",
            authority_id="authority-test",
            name="test",
            loaded_build_factory=loaded_factory if build else None,
        )
        node._lease_deadline = time.monotonic() + 45
        return node

    node = await make()
    host = node.executor.hosts.templates["local-build:build-test"]
    server.now = datetime.now(timezone.utc)
    native = native_command(
        payload={
            "content": "go",
            "teams_context_ref": "context-test",
            "execution_policy_ref": "policy-test",
            "execution_grant_id": "grant-test",
            "execution_grant_attempt_epoch": 1,
        },
        idempotency_key="original-key",
    )
    native = native.model_copy(update={"agent_instance_id": host.binding.agent_instance_id})
    ref = TeamsExecutionRef(
        authorityId="authority-test",
        groupId="group-test",
        teamRunId="run-test",
        memberId="member-test",
        runMemberId="run-member-test",
        taskId="task-test",
        attemptId="attempt-test",
        deliveryId="delivery-test",
        bindingRef="server-binding-test",
        providerRef="harness",
        sessionId=native.session_id,
        commandId=str(native.command_id),
        idempotencyKey=native.idempotency_key,
        schedulerEpoch=1,
        leaderEpoch=1,
        dispatchEpoch=1,
        attemptEpoch=1,
        target=host.binding.target,
        bundleDigest=host.binding.bundle_digest,
        contractDigest=host.binding.contract_digest,
        capabilitiesDigest=host.capabilities_digest,
    )
    facts = {
        "policy": {"systemContext": "trusted policy from Server", "limits": {"max_model_calls": 2}}
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
            tenant_id=native.tenant_id,
            agent_instance_id=native.agent_instance_id,
            session_id=native.session_id,
            owner_ref="host-test",
            attempt_epoch=1,
        ),
    )
    server.context, server.incarnation = context, await host.registry.incarnation()
    h = SimpleNamespace(
        context=context,
        now=server.now,
        signer=server.signer,
        native_authority=server.native_authority,
        loaded_build=build,
    )
    h.permit = lambda operation, **kwargs: HostHarness.permit(h, operation, **kwargs)
    holder = [node]
    try:
        yield node, h, server, reasoner, make, holder
    finally:
        await holder[0].close()
        await sessions.aclose()


async def test_real_factory_native_submit_policy_canonical_result_and_restart(setup):
    node, h, server, reasoner, make, holder = setup
    assert node.bindings[0]["capabilities"]["enqueue"] is True
    assert node.bindings[0]["capabilities"]["materialization"] is True
    prepared = await process(node, h, prepare(h))
    assert prepared.phase == "prepared", prepared
    order = submit(h)
    native = server.native_authority.permit(
        agent_instance_id=h.context.grant.agent_instance_id,
        permit_id="server-native-refreshed",
        issued_at=timestamp(h.now),
        expires_at=timestamp(h.now + timedelta(seconds=30)),
    )
    order = order.model_copy(
        update={
            "authorization": order.authorization.model_copy(
                update={
                    "nativePermit": native,
                    "nativeCommand": h.context.command.model_copy(
                        update={
                            "authorization_ref": native.permit_id,
                            "submitted_at": timestamp(h.now + timedelta(seconds=1)),
                        }
                    ),
                }
            )
        }
    )
    submitted = await process(node, h, order)
    assert submitted.phase == "submitted", submitted
    run_id = submitted.receipt.runId
    host = node.executor.hosts.hosts[(h.context.grant.agent_instance_id, h.context.ref.bindingRef)]
    output = None
    for _ in range(100):
        output = await host.get_execution_result(
            context_ref=h.context.context_ref,
            expected_incarnation=server.incarnation,
            permit=h.permit("get_result", recovery=True),
        )
        if output["status"] != "pending":
            break
        await asyncio.sleep(0.025)
    assert output["status"] == "succeeded", json.dumps(
        [
            e.payload
            for e in await host.events.read(h.context.ref.sessionId, 0, 100)
            if e.event_type == "run.failed"
        ]
    )
    assert output["candidate"]["result"] == "canonical production factory result"
    assert len(reasoner.calls) == 2
    assert len(server.invocations) == 1, json.dumps(
        [
            e.payload
            for e in await host.events.read(h.context.ref.sessionId, 0, 100)
            if e.event_type in {"item.completed", "run.failed"}
        ]
    )
    assert server.invocations[0]["commandId"] == h.context.ref.commandId
    request = StartRequest(
        input="go",
        user_id=h.context.owner_subject,
        session_id=h.context.ref.sessionId,
        agent_id=h.context.grant.agent_instance_id,
        metadata={
            "command_id": h.context.ref.commandId,
            "run_id": run_id,
            "execution_policy_ref": h.context.policy_ref,
            "teams_context_ref": h.context.context_ref,
        },
    )
    from ksadk.studio.teams_node_factory import _CURRENT_COMMAND

    assert _CURRENT_COMMAND.get() is None
    policy = await node.executor.hosts.resolve(h.context.policy_ref, request=request)
    assert policy.system_context == "trusted policy from Server"
    await node.flush_outbox()
    lookup = command(
        h,
        "lookup",
        {
            "commandId": h.context.ref.commandId,
            "idempotencyKey": h.context.ref.idempotencyKey,
            "payloadDigest": h.context.payload_digest,
            "storeIncarnation": server.incarnation,
        },
        permit=h.permit("lookup", recovery=True, allowedOperations=["lookup", "get_result"]),
    )
    completion = await process(node, h, lookup)
    assert completion.phase == "terminal", completion
    assert completion.executionResult.candidate.result == output["candidate"]["result"]
    assert completion.executionResult.terminalEvidence == completion.receipt.terminalEvidence
    server.lose_ack = True
    with pytest.raises(ConnectionError):
        await node.flush_outbox()
    before_restart = server.reports[-1]
    await node.close()
    restarted = await make()
    holder[0] = restarted
    # The original durable report is resent before any renewed submission.
    await restarted.flush_outbox()
    assert server.reports[-1] == before_restart
    assert server.reports[-1]["executionResult"]["completionDigest"] == (
        completion.executionResult.completionDigest
    )
    fresh = submit(h)
    report = await process(restarted, h, fresh)
    assert report.phase == "submitted", report
    assert report.receipt.runId == run_id
    assert len(reasoner.calls) == 2
    from ksadk.kernel.execution_grants import ExecutionGrantBlocked

    with pytest.raises(ExecutionGrantBlocked, match="clock_unverified"):
        await restarted.executor.hosts.resolve(h.context.policy_ref, request=request)
    assert len(server.context_requests) >= 1


async def test_real_node_probe_has_no_execution_ref_and_durable_ack_replay(setup):
    node, h, server, reasoner, _, _ = setup
    local_ref = "local-build:build-test"
    host = node.executor.hosts.templates[local_ref]
    expected = {
        "bundle": host.binding.bundle_digest,
        "contract": host.binding.contract_digest,
        "capabilities": host.capabilities_digest,
    }
    signed = sign_permit(
        BindingProbePermit(
            issuer="host-test",
            kid=server.signer.key_id,
            authorityId="authority-test",
            probeId=str(uuid4()),
            bindingRef="server-binding-test",
            target=host.binding.target,
            expectedDigests=expected,
            allowedOperations=["describe"],
            issuedAt=timestamp(h.now),
            expiresAt=timestamp(h.now + timedelta(seconds=30)),
            nonce=str(uuid4()),
        ),
        server.signer,
    )
    probe = NodeProbeCommand(
        nodeCommandId=str(uuid4()),
        operation="describe",
        operationKey="probe-key",
        authorityId="authority-test",
        nodeId="node-test",
        nodeGeneration=1,
        bindingRef="server-binding-test",
        localBindingRef=local_ref,
        expectedDigests=expected,
        claimLeaseUntil=timestamp(h.now + timedelta(seconds=30)),
        authorization={"permitKind": "probe", "permit": signed.model_dump_json()},
        commandDigest=digest("x"),
    )
    probe = probe.model_copy(update={"commandDigest": node_probe_digest(probe)})
    report = await process(node, h, probe)
    assert report.phase == "described", report
    assert report.probeResult.storeIncarnation == server.incarnation
    assert report.probeResult.agentInstanceId == h.context.grant.agent_instance_id
    assert "ref" not in report.model_dump()
    server.lose_ack = True
    with pytest.raises(ConnectionError):
        await node.flush_outbox()
    await node.flush_outbox()
    assert server.reports[-1] == server.reports[-2]
    assert not reasoner.calls and not server.context_requests


async def test_real_factory_writes_and_publishes_bounded_workspace_bytes(setup):
    import httpx

    from tests.kernel.test_teams_artifact_tools import ArtifactServer

    node, h, server, reasoner, make, holder = setup
    facts = {
        "policy": {"systemContext": "trusted policy from Server", "limits": {"max_model_calls": 6}}
    }
    h.context = h.context.model_copy(update={"context": facts, "context_digest": digest(facts)})
    server.context = h.context
    remote = ArtifactServer(h.context)
    original = server.request

    async def request(method, path, body=None):
        if path.endswith("/contexts/invoke") and body["operation"] == "team_publish_artifact":
            return await remote.invoke(body["operation"], body["arguments"], body["callId"])
        return await original(method, path, body)

    server.request = request

    initial_unmapped = True

    async def handler(req):
        nonlocal initial_unmapped
        assert req.headers["x-teams-node-token"] == node.identity["accessToken"]
        assert req.url.params["contextRef"] == h.context.context_ref
        path = req.url.path.split("/nodes/node-test", 1)[1]
        if path == "/artifacts":
            if initial_unmapped:
                initial_unmapped = False
                return httpx.Response(409, json={"error": {"code": "original_run_unverified"}})
            result = await remote.create_artifact(h.context, json.loads(await req.aread()))
        elif req.method == "PUT":
            data = await req.aread()

            async def chunks():
                yield data

            await remote.upload_artifact(
                h.context, path.split("/")[2], chunks(), size_bytes=len(data)
            )
            result = {"state": "ready"}
        else:
            assert path.endswith("/finalize")
            result = await remote.finalize_artifact(
                h.context,
                path.split("/")[2],
                idempotency_key=json.loads(await req.aread())["idempotencyKey"],
            )
        return httpx.Response(200, json=result)

    transport = node.executor.hosts.material_transport.client
    await transport.http.aclose()
    transport.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    count = 0

    async def complete(*, tools, messages, **kwargs):
        nonlocal count
        count += 1
        assert "write_file" in {t.name for t in tools}
        assert "shell" not in {t.name for t in tools}
        if count == 1:
            call = HarnessToolCall(
                "write-proof", "write_file", {"path": "report.md", "content": "verified report"}
            )
        elif count == 2:
            call = HarnessToolCall("publish-proof", "team_publish_artifact", {"path": "report.md"})
        else:
            return HarnessReasoningTurn(final_text="Report published with verified bytes")
        return HarnessReasoningTurn(tool_calls=(call,))

    reasoner.complete = complete
    assert (await process(node, h, prepare(h))).phase == "prepared"
    order = submit(h)
    permit = server.native_authority.permit(
        agent_instance_id=h.context.grant.agent_instance_id,
        issued_at=timestamp(h.now),
        expires_at=timestamp(h.now + timedelta(seconds=30)),
    )
    order = order.model_copy(
        update={
            "authorization": order.authorization.model_copy(
                update={
                    "nativePermit": permit,
                    "nativeCommand": h.context.command.model_copy(
                        update={"authorization_ref": permit.permit_id}
                    ),
                }
            )
        }
    )
    submitted = await process(node, h, order)
    assert submitted.phase == "submitted", submitted
    host = node.executor.hosts.hosts[(h.context.grant.agent_instance_id, h.context.ref.bindingRef)]
    for _ in range(100):
        output = await host.get_execution_result(
            context_ref=h.context.context_ref,
            expected_incarnation=server.incarnation,
            permit=h.permit("get_result", recovery=True),
        )
        if output["status"] != "pending":
            break
        await asyncio.sleep(0.025)
    assert output["status"] == "succeeded", output
    assert len(remote.objects) == 1
    assert list(remote.objects.values()) == [b"verified report"]
    assert remote.invocations[0][1]["path"].startswith("tar_")


@pytest.mark.parametrize("setup", [True], indirect=True)
async def test_real_node_factory_reads_actual_build_on_every_descriptor(setup):
    from ksadk.kernel.teams_execution_context import ContextConflict

    node, h, _, _, _, _ = setup
    hosts = node.executor.hosts
    host = hosts._bound_host(hosts.templates["local-build:build-test"], h.context.ref.bindingRef)
    original = await hosts.descriptor(host)
    assert original["capabilities"]["loadedBuild"]["codeArtifactDigest"] == (
        h.loaded_build.archive.code_digest
    )
    assert original["capabilitiesDigest"] == digest(original["capabilities"])
    assert node.bindings[0]["capabilitiesDigest"] == original["capabilitiesDigest"]
    host._binding_check(h.context)
    h.loaded_build.workspace.chmod(0o755)
    changed = await hosts.descriptor(host)
    assert "loadedBuild" not in changed["capabilities"]
    assert changed["capabilities"]["teamsReady"] is True
    with pytest.raises(ContextConflict, match="capability digest mismatch"):
        host._binding_check(h.context)
