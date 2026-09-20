"""Real SQLite/PostgreSQL Kernel and signed Host fixture helpers."""

import base64
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.harness.execution_policy import ExecutionPolicy
from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.execution_grants import ExecutionGrantSpec
from ksadk.kernel.execution_host_ingress import (
    HostBinding,
    KernelTeamsExecutionHost,
    TrustedGrantWindow,
    TrustedPreparation,
)
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.teams_execution_context import (
    PostgresTeamsExecutionContextRegistry,
    PreparedTeamsContext,
    SQLiteTeamsExecutionContextRegistry,
)
from ksadk.plugins.teams.cloud_contracts import TeamsExecutionRef, digest
from ksadk.plugins.teams.cloud_permits import (
    TeamsExecutionPermit,
    TeamsPermitVerifier,
    sign_permit,
    timestamp,
)
from ksadk.sessions.local_service import LocalSessionService
from tests.kernel.control_harness import PermitAuthority as NativePermitAuthority
from tests.kernel.control_harness import command, default_matrix, native


@pytest.fixture(scope="module")
def temporary_postgres(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    previous = {key: os.environ.get(key) for key in ("LC_ALL", "LANG")}
    os.environ.update(LC_ALL="C", LANG="C")
    server = pgserver.get_server(
        tmp_path_factory.mktemp("teams-host-pg") / "db", cleanup_mode="stop"
    )
    try:
        yield server
    finally:
        server.cleanup()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class PermitAuthority(NativePermitAuthority):
    def permit(self, **kwargs):
        kwargs.setdefault("subject_ref", "owner-test")
        return super().permit(**kwargs)


class Signer:
    key_id = "host-test-key"

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()

    def sign(self, value):
        return base64.urlsafe_b64encode(self.private.sign(value)).decode().rstrip("=")


@dataclass
class HostHarness:
    backend: str
    host: KernelTeamsExecutionHost
    context: PreparedTeamsContext
    registry: object
    store: object
    events: object
    sessions: object
    signer: Signer
    native_authority: PermitAuthority
    incarnation: str
    now: datetime
    preparation: TrustedPreparation

    def permit(self, operation, *, recovery=False, revision=1, **changes):
        context = self.context
        unsigned = TeamsExecutionPermit(
            issuer="host-test",
            kid=self.signer.key_id,
            authorityId=context.ref.authorityId,
            subjectRef=context.owner_subject,
            agentInstanceId=context.grant.agent_instance_id,
            sessionId=context.ref.sessionId,
            commandId=context.ref.commandId,
            allowedOperations=[operation],
            permitKind="recovery" if recovery else "execute",
            payloadDigest=context.payload_digest,
            policyDigest=context.policy_digest,
            leaderEpoch=1,
            dispatchEpoch=1,
            attemptEpoch=1,
            grantRevision=revision,
            issuedAt=timestamp(self.now),
            expiresAt=timestamp(self.now + timedelta(seconds=10)),
            nonce=str(uuid4()),
        )
        if changes:
            unsigned = TeamsExecutionPermit.model_validate(unsigned.model_dump() | changes)
        return sign_permit(unsigned, self.signer)

    async def prepare(self):
        return await self.host.prepare_execution(
            context_ref=self.context.context_ref,
            expected_incarnation=self.incarnation,
            permit=self.permit("prepare"),
        )

    async def submit(self):
        native_permit = self.native_authority.permit(
            issued_at=timestamp(self.now),
            expires_at=timestamp(self.now + timedelta(seconds=60)),
        )
        return await self.host.submit_execution(
            self.context.command,
            expected_incarnation=self.incarnation,
            permit=self.permit("enqueue"),
            native_permit=native_permit,
        )

    async def lookup(self, **changes):
        values = dict(
            context_ref=self.context.context_ref,
            expected_incarnation=self.incarnation,
            permit=self.permit("lookup", recovery=True),
            command_id=self.context.ref.commandId,
            idempotency_key=self.context.ref.idempotencyKey,
            payload_digest=self.context.payload_digest,
        )
        return await self.host.lookup_execution(**(values | changes))


@pytest.fixture(params=["sqlite", "postgres"])
async def host_harness(request, tmp_path):
    backend = request.param
    cleanup = None
    if backend == "postgres":
        import asyncpg

        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresFencedSessionEventStore,
        )
        from ksadk.sessions.postgres_service import PostgresSessionService

        server = request.getfixturevalue("temporary_postgres")
        admin = await asyncpg.connect(server.get_uri())
        name = "host_test_" + uuid4().hex
        await admin.execute(f'CREATE DATABASE "{name}"')
        sessions = PostgresSessionService(dsn=server.get_uri(name))
        await sessions.create_session(agent_id="agent-1", user_id="owner-test", session_id="s1")
        store = PostgresAgentKernelStore(sessions._pool, None, tenant_id="tenant-1")
        await store.ensure_schema()
        events = PostgresFencedSessionEventStore(store)
        registry = PostgresTeamsExecutionContextRegistry(store)
        cleanup = (admin, name)
    else:
        sessions = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
        await sessions.create_session(agent_id="agent-1", user_id="owner-test", session_id="s1")
        events = SessionServiceEventStore(sessions)
        store = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", events)
        await store.ensure_schema()
        registry = SQLiteTeamsExecutionContextRegistry(store)
    incarnation = await registry.initialize()
    now = datetime.now(timezone.utc)
    signer, native_authority = Signer(), PermitAuthority()
    native_matrix = default_matrix().model_copy(
        update={"execution_policy": native(), "cancel": native()}
    )
    kernel = AgentKernel(
        store,
        events,
        AgentControlPermitVerifier(native_authority.jwks()),
        capabilities=lambda: native_matrix,
    )

    async def ensure(context):
        return await sessions.create_session(
            agent_id=context.grant.agent_instance_id,
            user_id=context.owner_subject,
            session_id=context.ref.sessionId,
        )

    async def policy_factory(context, **kwargs):
        return ExecutionPolicy(system_context="trusted policy")

    async def loader(ref):
        return harness.preparation

    binding = HostBinding(
        authority_id="authority-test",
        binding_ref="binding-test",
        provider_ref="provider-test",
        agent_instance_id="agent-1",
        target={"kind": "node", "nodeId": "node-test", "nodeGeneration": 1},
        bundle_digest=digest("bundle"),
        contract_digest=digest("contract"),
    )
    host = KernelTeamsExecutionHost(
        kernel=kernel,
        store=store,
        events=events,
        registry=registry,
        binding=binding,
        permit_verifier=TeamsPermitVerifier(
            {signer.key_id: signer.private.public_key()}, issuer="host-test"
        ),
        context_loader=loader,
        session_ensurer=ensure,
        policy_factory=policy_factory,
        policy_enforcement_ready=True,
        canonical_events_durable=True,
        canonical_events_shared=backend == "postgres",
    )
    grant = ExecutionGrantSpec(
        grant_id="grant-test",
        tenant_id="tenant-1",
        agent_instance_id="agent-1",
        session_id="s1",
        owner_ref="host-test",
        attempt_epoch=1,
    )
    cmd = command(
        payload={
            "content": "frozen task",
            "execution_grant_id": grant.grant_id,
            "execution_grant_attempt_epoch": 1,
            "execution_policy_ref": "policy-test",
            "teams_context_ref": "context-test",
        },
        idempotency_key="original-key",
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
        bindingRef=binding.binding_ref,
        providerRef=binding.provider_ref,
        sessionId=cmd.session_id,
        commandId=str(cmd.command_id),
        idempotencyKey=cmd.idempotency_key,
        schedulerEpoch=1,
        leaderEpoch=1,
        dispatchEpoch=1,
        attemptEpoch=1,
        target=binding.target,
        bundleDigest=binding.bundle_digest,
        contractDigest=binding.contract_digest,
        capabilitiesDigest=host.capabilities_digest,
    )
    context = PreparedTeamsContext(
        context_ref="context-test",
        context_digest=digest({"role": "worker"}),
        policy_ref="policy-test",
        policy_digest=digest("policy"),
        owner_subject="owner-test",
        ref=ref,
        grant=grant,
        command=cmd,
        context={"role": "worker"},
    )
    preparation = TrustedPreparation(
        context, TrustedGrantWindow(timestamp(now + timedelta(seconds=20)), now, time.monotonic())
    )
    harness = HostHarness(
        backend,
        host,
        context,
        registry,
        store,
        events,
        sessions,
        signer,
        native_authority,
        incarnation,
        now,
        preparation,
    )
    try:
        yield harness
    finally:
        await store.close()
        if cleanup:
            await sessions.aclose()
            admin, name = cleanup
            await admin.execute(f'DROP DATABASE "{name}"')
            await admin.close()
