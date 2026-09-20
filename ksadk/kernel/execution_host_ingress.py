"""Trusted teams-host/v1 service over the existing Kernel, never a model path.

Production bootstrap must supply internal service authentication, trusted context
loading, a mounted policy resolver and the actual durable event store. This
module deliberately provides no unauthenticated/default HTTP router.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import parse_runtime_event
from ksadk.harness.execution_policy import ExecutionPolicy
from ksadk.kernel.contracts import AgentControlCommand, AgentControlPermit
from ksadk.kernel.execution_grants import execution_grant_run_id
from ksadk.kernel.store import command_digest
from ksadk.kernel.teams_execution_context import (
    ContextConflict,
    PreparedTeamsContext,
    StoreIdentityMismatch,
    TeamsExecutionContextRegistry,
)
from ksadk.plugins.teams.cloud_contracts import (
    CanonicalEventBatch,
    CanonicalExecutionEvent,
    EventSourceRef,
    ExecutionGrantBarrierView,
    ExecutionGrantSnapshot,
    ExecutionGrantView,
    GetGrantResult,
    GrantCommandView,
    GrantMutationReceipt,
    HostReceipt,
    MaterialProof,
    PrepareResult,
    SetAdmissionResult,
    SetGrantResult,
    TerminalEvidence,
    digest,
)
from ksadk.plugins.teams.cloud_permits import (
    BindingProbePermit,
    PermitError,
    TeamsExecutionPermit,
    TeamsPermitVerifier,
    timestamp,
)

TEAMS_HOST_PROTOCOL = "teams-host/v1"
_TERMINAL = {
    "run.completed": "succeeded",
    "run.failed": "failed",
    "run.canceled": "cancelled",
    "run.interrupted": "interrupted",
}
_RUN_STATUS = {
    "pending": "queued",
    "running": "running",
    "paused": "waiting",
    "waiting": "awaiting_approval",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def grant_snapshot(barrier, store_incarnation: str) -> ExecutionGrantSnapshot:
    """Translate existing Kernel facts without introducing another state machine."""
    grant = barrier.grant
    if grant.attempt_epoch is None or grant.expires_at is None:
        raise ContextConflict("Teams requires an expiring, attempt-scoped grant")
    return ExecutionGrantSnapshot(
        storeIncarnation=store_incarnation,
        grant=ExecutionGrantView(
            grantId=grant.grant_id,
            state=grant.state,
            revision=grant.revision,
            attemptEpoch=grant.attempt_epoch,
            expiresAt=timestamp(_time(grant.expires_at)),
            admissionAllowed=grant.admission_allowed,
            admissionRevision=grant.admission_revision,
        ),
        barrier=ExecutionGrantBarrierView(
            queuedMessageIds=list(barrier.queued_message_ids),
            inFlightMessageIds=list(barrier.in_flight_message_ids),
            discardedMessageIds=list(barrier.discarded_message_ids),
            settledMessageIds=list(barrier.settled_message_ids),
            commands=[
                GrantCommandView(
                    messageId=item.message_id,
                    commandId=item.command_id,
                    idempotencyKey=item.idempotency_key,
                    inboxState=item.inbox_state,
                    runId=item.run_id,
                    runState=item.run_state,
                )
                for item in barrier.commands
            ],
        ),
    )


@dataclass(frozen=True)
class TrustedGrantWindow:
    """Internal input from verified Server time; never parse an untrusted body.

    Capture request_started_monotonic before the Server request. Response delay
    and any time spent waiting for a DB lock count against the authorized TTL.
    """

    expires_at: str
    server_time: datetime
    request_started_monotonic: float
    safety_margin_seconds: float = 5.0
    # Transport-only permission from the authenticated Server response. It is
    # deliberately outside the immutable context and never grants a clock.
    callback_permit: Any = None

    def remaining(self, monotonic: Callable[[], float] = time.monotonic) -> float:
        if self.server_time.tzinfo is None or self.safety_margin_seconds < 0:
            raise ValueError("a trusted UTC grant window is required")
        elapsed = monotonic() - self.request_started_monotonic
        ttl = (_time(self.expires_at) - self.server_time).total_seconds()
        if elapsed < 0 or not 0 < ttl <= 30:
            raise PermitError("invalid trusted grant time window")
        remaining = ttl - elapsed - self.safety_margin_seconds
        if remaining <= 0:
            raise PermitError("trusted grant time window expired in transit")
        return remaining


@dataclass(frozen=True)
class TrustedPreparation:
    context: PreparedTeamsContext
    grant_window: TrustedGrantWindow


@dataclass(frozen=True)
class HostBinding:
    authority_id: str
    binding_ref: str
    provider_ref: str
    agent_instance_id: str
    target: dict[str, Any]
    bundle_digest: str
    contract_digest: str


class KernelTeamsExecutionHost:
    """Injectable service; all execution still enters AgentKernel.submit.

    A cloud binding is admissible only with a shared registry/event store and
    the actual runtime policy resolver mounted. SQLite remains a local option.
    The capability flags are deployment evidence, not inferred from methods.
    """

    def __init__(
        self,
        *,
        kernel,
        store,
        events,
        registry: TeamsExecutionContextRegistry,
        binding: HostBinding,
        permit_verifier: TeamsPermitVerifier,
        context_loader: Callable[[str], Awaitable[TrustedPreparation]],
        session_ensurer: Callable[[PreparedTeamsContext], Awaitable[Any]] | None = None,
        policy_factory: Callable[..., Awaitable[ExecutionPolicy]] | None = None,
        material_preparer: Callable[[PreparedTeamsContext], Awaitable[MaterialProof]] | None = None,
        renewal_loader: Callable[[str, str], Awaitable[TrustedGrantWindow]] | None = None,
        callback_registrar: Callable[..., Awaitable[None]] | None = None,
        effect_journal=None,
        effect_adapters=None,
        loaded_build_provider=None,
        teams_tools_ready: bool = False,
        callback_ready: bool = False,
        policy_enforcement_ready: bool = False,
        canonical_events_durable: bool = False,
        canonical_events_shared: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (
            registry.kernel_store is not store
            or kernel._store is not store
            or kernel._events is not events
        ):
            raise ValueError("registry and Host must use the actual Kernel store and event log")
        self.kernel, self.store, self.events, self.registry = kernel, store, events, registry
        self.binding, self.verifier, self.loader = binding, permit_verifier, context_loader
        self.session_ensurer, self.policy_factory = session_ensurer, policy_factory
        self.material_preparer = material_preparer
        self.renewal_loader = renewal_loader
        self.effect_journal = effect_journal
        self.effect_adapters = dict(effect_adapters or {})
        if loaded_build_provider is not None:
            from ksadk.plugins.teams.build_artifacts import LoadedBuildProvider

            if not isinstance(loaded_build_provider, LoadedBuildProvider):
                raise ValueError("loaded build evidence requires a trusted filesystem verifier")
        self.loaded_build_provider = loaded_build_provider
        self.teams_tools_ready, self.callback_ready = teams_tools_ready, callback_ready
        self.callback_registrar = callback_registrar
        self.policy_enforcement_ready = policy_enforcement_ready
        self.events_durable, self.events_shared = canonical_events_durable, canonical_events_shared
        self.clock, self.monotonic = clock, monotonic

    def capability_snapshot(self) -> dict[str, Any]:
        native = self.kernel.capabilities()
        policy = bool(
            native.execution_policy
            and native.execution_policy.supported
            and self.policy_factory is not None
            and self.policy_enforcement_ready
        )
        durable = bool(self.registry.durable and self.events_durable)
        shared = bool(self.registry.shared_across_hosts and self.events_shared)
        from ksadk.kernel.teams_tool_runtime import adapter_capabilities

        capabilities = {
            "teamsTools": self.teams_tools_ready and policy,
            "callbackReady": self.callback_ready,
            "effectLedger": self.effect_journal is not None
            and (
                self.binding.target.get("kind") != "cloud_agent"
                or self.effect_journal.shared_across_hosts
            ),
            "effectAdapters": adapter_capabilities(self.effect_adapters),
            "protocol": TEAMS_HOST_PROTOCOL,
            "runtime": native.model_dump(mode="json"),
            "durableLookup": bool(self.registry.durable),
            "canonicalResult": self.events_durable,
            "materialPreparation": self.material_preparer is not None,
            "executionPolicy": policy,
            "expiringGrant": True,
            "admissionBarrier": True,
            "ensureSession": self.session_ensurer is not None,
            "sharedAcrossHosts": shared,
            "teamsReady": policy
            and durable
            and self.session_ensurer is not None
            and (self.binding.target.get("kind") != "cloud_agent" or shared),
        }
        if self.loaded_build_provider is not None:
            try:
                proof = self.loaded_build_provider()
            except (OSError, ValueError):
                # Losing immutable-source evidence invalidates the old digest.
                # Ordinary Teams capability stays available without standby.
                pass
            else:
                capabilities["loadedBuild"] = proof.model_dump(mode="json")
        return capabilities

    @property
    def capabilities_digest(self) -> str:
        return digest(self.capability_snapshot())

    def _binding_check(self, context: PreparedTeamsContext) -> None:
        ref, binding = context.ref, self.binding
        if any(
            (
                ref.authorityId != binding.authority_id,
                ref.bindingRef != binding.binding_ref,
                ref.providerRef != binding.provider_ref,
                context.grant.agent_instance_id != binding.agent_instance_id,
                ref.target.model_dump(mode="json") != binding.target,
                ref.bundleDigest != binding.bundle_digest,
                ref.contractDigest != binding.contract_digest,
                ref.capabilitiesDigest != self.capabilities_digest,
            )
        ):
            raise ContextConflict("original runtime binding or capability digest mismatch")

    async def describe_binding(self, *, permit: BindingProbePermit | dict) -> dict[str, Any]:
        # Probe id is a caller nonce, not authority. All target/digest claims
        # are compared with the host's deployment and actual runtime snapshot.
        probe = BindingProbePermit.model_validate(permit)
        capabilities = self.capability_snapshot()
        self.verifier.verify(
            probe,
            BindingProbePermit,
            operation="describe",
            now=self.clock(),
            expected={
                "authorityId": self.binding.authority_id,
                "probeId": probe.probeId,
                "bindingRef": self.binding.binding_ref,
                "target": self.binding.target,
                "expectedDigests": {
                    "bundle": self.binding.bundle_digest,
                    "contract": self.binding.contract_digest,
                    "capabilities": digest(capabilities),
                },
            },
        )
        return {
            "capabilities": capabilities,
            "capabilitiesDigest": digest(capabilities),
            "contractDigest": self.binding.contract_digest,
            "bundleDigest": self.binding.bundle_digest,
            "storeIncarnation": await self.registry.incarnation(),
        }

    def _verify(self, context, permit, operation, *, revision, expires_at=None):
        ref = context.ref
        return self.verifier.verify(
            permit,
            TeamsExecutionPermit,
            operation=operation,
            now=self.clock(),
            grant_expires_at=_time(expires_at) if expires_at else None,
            expected={
                "authorityId": ref.authorityId,
                "subjectRef": context.owner_subject,
                "agentInstanceId": context.grant.agent_instance_id,
                "sessionId": ref.sessionId,
                "commandId": ref.commandId,
                "payloadDigest": context.payload_digest,
                "policyDigest": context.policy_digest,
                "attemptEpoch": ref.attemptEpoch,
                "leaderEpoch": ref.leaderEpoch,
                "dispatchEpoch": ref.dispatchEpoch,
                "grantRevision": revision,
            },
        )

    async def _context(self, context_ref: str, expected_incarnation: str) -> PreparedTeamsContext:
        context = await self.registry.get(context_ref, expected_incarnation=expected_incarnation)
        if context is None:
            # Absence of trusted context is not proof that an old execution is missing.
            raise StoreIdentityMismatch("original prepared context unavailable")
        self._binding_check(context)
        return context

    async def _authorized(self, context_ref, expected_incarnation, permit, operation):
        context = await self._context(context_ref, expected_incarnation)
        barrier = await self.kernel.get_execution_grant(context.grant)
        parsed = TeamsExecutionPermit.model_validate(permit)
        if barrier is None and parsed.permitKind == "execute":
            raise ContextConflict("prepared execution grant unavailable")
        # Recovery's signed grantRevision is the Server's observation, not a
        # current execution fence. Lost renewal ACKs make the current revision
        # unknown to the caller. Recovery still verifies every immutable scope
        # claim and cannot enqueue, approve, renew or reactivate the grant.
        revision = (
            parsed.grantRevision if parsed.permitKind == "recovery" else barrier.grant.revision
        )
        verified = self._verify(
            context,
            parsed,
            operation,
            revision=revision,
            expires_at=barrier.grant.expires_at if barrier else None,
        )
        if verified.permitKind == "execute" and operation != "renew_grant":
            await self.kernel.require_execution_grant(context.grant)
        # A fresh authorized renewal may restore a missing restart clock anchor;
        # the Kernel still rejects an elapsed/revoked grant and CAS conflicts.
        return context, barrier

    async def prepare_execution(self, *, context_ref, expected_incarnation, permit):
        if not self.capability_snapshot()["teamsReady"]:
            raise ContextConflict("runtime Teams enforcement is not ready")
        preparation = await self.loader(context_ref)
        context = PreparedTeamsContext.model_validate_json(preparation.context.model_dump_json())
        if context.context_ref != context_ref:
            raise ContextConflict("trusted loader returned a different context")
        self._binding_check(context)
        existing = await self.kernel.get_execution_grant(context.grant)
        revision = existing.grant.revision if existing else 1
        expires_at = existing.grant.expires_at if existing else preparation.grant_window.expires_at
        self._verify(context, permit, "prepare", revision=revision, expires_at=expires_at)
        preparation.grant_window.remaining(self.monotonic)
        await self.registry.put(context, expected_incarnation=expected_incarnation)
        await self._ensure_owned_session(context)
        if context.material_manifest_ref is not None:
            if self.material_preparer is None:
                raise ContextConflict("trusted material verification is not configured")
            proof = MaterialProof.model_validate(await self.material_preparer(context))
            if (
                proof.manifestRef != context.material_manifest_ref
                or proof.digest != context.material_manifest_digest
            ):
                raise ContextConflict("material proof differs from frozen manifest")
        await self.kernel.ensure_execution_grant(
            context.grant,
            expires_at=preparation.grant_window.expires_at,
            remaining_ttl_seconds=preparation.grant_window.remaining(self.monotonic),
        )
        # Replayed prepare cannot revive a process-local monotonic deadline.
        await self.kernel.require_execution_grant(context.grant)
        barrier = await self.kernel.get_execution_grant(context.grant)
        if self.callback_registrar is not None:
            await self.callback_registrar(context, preparation.grant_window, barrier)
        return PrepareResult(
            operation="prepare",
            contextRef=context_ref,
            contextDigest=context.context_digest,
            snapshot=grant_snapshot(barrier, expected_incarnation),
        )

    async def ensure_session(self, *, context_ref, expected_incarnation, permit):
        # Separate operation, using the same trusted reservation as prepare.
        if self.session_ensurer is None:
            raise ContextConflict("trusted session creation is not configured")
        preparation = await self.loader(context_ref)
        context = preparation.context
        if context.context_ref != context_ref:
            raise ContextConflict("trusted loader returned a different context")
        self._binding_check(context)
        existing = await self.kernel.get_execution_grant(context.grant)
        self._verify(
            context,
            permit,
            "ensure_session",
            revision=existing.grant.revision if existing else 1,
            expires_at=existing.grant.expires_at
            if existing
            else preparation.grant_window.expires_at,
        )
        if existing:
            await self.kernel.require_execution_grant(context.grant)
        preparation.grant_window.remaining(self.monotonic)
        await self.registry.put(context, expected_incarnation=expected_incarnation)
        await self._ensure_owned_session(context)
        return {"sessionId": context.ref.sessionId, "storeIncarnation": expected_incarnation}

    async def _ensure_owned_session(self, context):
        session = await self.session_ensurer(context)
        if (
            getattr(session, "id", None) != context.ref.sessionId
            or getattr(session, "user_id", None) != context.owner_subject
            or getattr(session, "agent_id", None) != context.grant.agent_instance_id
        ):
            raise ContextConflict("session owner or instance differs from trusted preparation")

    async def validate_enqueue(self, command, *, expected_incarnation, permit):
        command = AgentControlCommand.model_validate(command)
        context, _ = await self._authorized(
            command.payload.get("teams_context_ref"), expected_incarnation, permit, "enqueue"
        )
        if str(command.command_id) != context.ref.commandId or command_digest(
            command
        ) != command_digest(context.command):
            raise ContextConflict("enqueue must preserve the original prepared command")
        return command

    async def lookup_before_submit(self, command, *, expected_incarnation, permit):
        """Read only the signed enqueue's original inbox before attempting admission.

        This internal submit step deliberately does not rebuild/require a local
        clock anchor: after restart an already accepted command remains readable.
        A missing receipt never authorizes execution; submit_execution still
        performs the full live grant check and native permit verification.
        """
        command = AgentControlCommand.model_validate(command)
        context = await self._context(
            command.payload.get("teams_context_ref"), expected_incarnation
        )
        barrier = await self.kernel.get_execution_grant(context.grant)
        if barrier is None:
            raise ContextConflict("prepared execution grant unavailable")
        self._verify(
            context,
            permit,
            "enqueue",
            revision=barrier.grant.revision,
            expires_at=barrier.grant.expires_at,
        )
        if str(command.command_id) != context.ref.commandId or command_digest(
            command
        ) != command_digest(context.command):
            raise ContextConflict("enqueue must preserve the original prepared command")
        return await self._lookup(context, expected_incarnation)

    async def validate_native_authorization(
        self, native_permit, *, context_ref, expected_incarnation
    ):
        """The two independently signed permits must name the same owner/session.

        The native Kernel still verifies signature, lifetime, nonce and allowed
        operation. Instance-wide permits cannot be combined with a Teams permit
        issued for a different principal or session.
        """
        context = await self._context(context_ref, expected_incarnation)
        native_permit = AgentControlPermit.model_validate(native_permit)
        if (
            native_permit.subject_ref != context.owner_subject
            or native_permit.session_id != context.ref.sessionId
            or native_permit.tenant_id != context.grant.tenant_id
            or native_permit.agent_instance_id != context.grant.agent_instance_id
        ):
            raise PermitError("native and Teams permit scopes differ")
        return native_permit

    async def submit_execution(
        self, command, *, expected_incarnation, permit, native_permit: AgentControlPermit
    ):
        command = await self.validate_enqueue(
            command, expected_incarnation=expected_incarnation, permit=permit
        )
        native_permit = await self.validate_native_authorization(
            native_permit,
            context_ref=command.payload["teams_context_ref"],
            expected_incarnation=expected_incarnation,
        )
        # Native permit verification, inbox idempotency and worker claim are unchanged.
        return await self.kernel.submit(command, permit=native_permit)

    async def lookup_execution(
        self,
        *,
        context_ref,
        expected_incarnation,
        permit,
        command_id,
        idempotency_key,
        payload_digest,
    ):
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, "lookup")
        if (command_id, idempotency_key, payload_digest) != (
            context.ref.commandId,
            context.ref.idempotencyKey,
            context.payload_digest,
        ):
            raise ContextConflict("lookup must use the original command, key and digest")
        return await self._lookup(context, expected_incarnation)

    async def _lookup(self, context, incarnation):
        message = await self.store.load_by_idempotency(
            context.ref.sessionId, context.ref.idempotencyKey
        )
        if await self.registry.incarnation() != incarnation:
            raise StoreIdentityMismatch("Kernel store identity changed during lookup")
        values = {
            "commandId": context.ref.commandId,
            "idempotencyKey": context.ref.idempotencyKey,
            "payloadDigest": context.payload_digest,
            "storeIncarnation": incarnation,
        }
        if message is None:
            return HostReceipt(status="missing", **values)
        if (
            message.command is None
            or message.agent_instance_id != context.grant.agent_instance_id
            or str(message.command.command_id) != context.ref.commandId
            or "sha256:" + message.request_digest != context.payload_digest
        ):
            raise ContextConflict("original inbox identity or payload differs")
        run_id = execution_grant_run_id(context.command)
        run = await self.store.load_run(run_id)
        if run is not None and (
            run.session_id != context.ref.sessionId
            or run.agent_instance_id != context.grant.agent_instance_id
        ):
            raise ContextConflict("original run scope differs")
        return HostReceipt(
            status="accepted",
            runId=run_id,
            acceptedSeq=message.accepted_seq,
            nativeStatus=_RUN_STATUS.get(str(run.state)) if run else "queued",
            **values,
        )

    async def lookup_control(self, *, context_ref, expected_incarnation, permit, command):
        """Read the original control inbox without resubmitting or claiming it.

        The context permit proves the original execution. The independent
        command/key/digest identify the control within its exact session/run.
        An inbox ACK carries no terminal result or authority to execute again.
        """
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, "lookup")
        command = AgentControlCommand.model_validate(command)
        run_id = execution_grant_run_id(context.command)
        if (
            command.command_type not in {"interrupt", "submit_interaction"}
            or command.tenant_id != context.grant.tenant_id
            or command.agent_instance_id != context.grant.agent_instance_id
            or command.session_id != context.ref.sessionId
            or command.payload.get("run_id") != run_id
            or str(command.command_id) == context.ref.commandId
            or command.idempotency_key == context.ref.idempotencyKey
        ):
            raise ContextConflict("control lookup must target the exact original execution")
        run = await self.store.load_run(run_id)
        if (
            run is None
            or run.session_id != context.ref.sessionId
            or run.agent_instance_id != context.grant.agent_instance_id
            or run.metadata.get("command_id") != context.ref.commandId
        ):
            raise ContextConflict("control lookup original run is unavailable")
        message = await self.store.load_by_idempotency(
            context.ref.sessionId, command.idempotency_key
        )
        if await self.registry.incarnation() != expected_incarnation:
            raise StoreIdentityMismatch("Kernel store identity changed during control lookup")
        fingerprint = command_digest(command)
        values = dict(
            commandId=str(command.command_id),
            idempotencyKey=command.idempotency_key,
            payloadDigest="sha256:" + fingerprint,
            storeIncarnation=expected_incarnation,
        )
        if message is None:
            return HostReceipt(status="missing", **values)
        if (
            message.command is None
            or message.agent_instance_id != command.agent_instance_id
            or message.session_id != command.session_id
            or str(message.command.command_id) != str(command.command_id)
            or message.request_digest != fingerprint
            or command_digest(message.command) != fingerprint
        ):
            raise ContextConflict("original control inbox identity or payload differs")
        return HostReceipt(
            status="accepted", runId=run_id, acceptedSeq=message.accepted_seq, **values
        )

    async def get_execution_grant(
        self, *, context_ref, expected_incarnation, permit, renewal_id=None
    ):
        context, barrier = await self._authorized(
            context_ref, expected_incarnation, permit, "get_grant"
        )
        receipt = None
        if renewal_id is not None:
            receipt = await self.kernel.lookup_execution_grant_operation(
                context.grant, idempotency_key=renewal_id
            )
        return GetGrantResult(
            operation="get_grant",
            storeIncarnation=expected_incarnation,
            current=grant_snapshot(barrier, expected_incarnation) if barrier else None,
            lookupOperationId=renewal_id,
            mutationReceipt=GrantMutationReceipt(
                operationId=renewal_id, snapshot=grant_snapshot(receipt, expected_incarnation)
            )
            if receipt
            else None,
        )

    async def renew_execution_grant(
        self,
        *,
        context_ref,
        expected_incarnation,
        permit,
        expected_revision,
        renewal_id,
        trusted_window: TrustedGrantWindow,
    ):
        context, _ = await self._authorized(
            context_ref, expected_incarnation, permit, "renew_grant"
        )
        barrier = await self.kernel.renew_execution_grant(
            context.grant,
            expected_revision=expected_revision,
            renewal_id=renewal_id,
            expires_at=trusted_window.expires_at,
            remaining_ttl_seconds=trusted_window.remaining(self.monotonic),
        )
        if self.callback_registrar is not None:
            await self.callback_registrar(context, trusted_window, barrier)
        return SetGrantResult(
            operation="set_grant",
            mutationReceipt=GrantMutationReceipt(
                operationId=renewal_id, snapshot=grant_snapshot(barrier, expected_incarnation)
            ),
        )

    async def set_execution_grant(
        self, *, context_ref, expected_incarnation, permit, state, expected_revision, control_id
    ):
        operation = "revoke" if state == "revoked" else "set_grant"
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, operation)
        barrier = await self.kernel.set_execution_grant_state(
            context.grant, state, expected_revision=expected_revision, idempotency_key=control_id
        )
        return SetGrantResult(
            operation="set_grant",
            mutationReceipt=GrantMutationReceipt(
                operationId=control_id, snapshot=grant_snapshot(barrier, expected_incarnation)
            ),
        )

    async def set_execution_admission(
        self, *, context_ref, expected_incarnation, permit, allowed, expected_revision, control_id
    ):
        context, _ = await self._authorized(
            context_ref, expected_incarnation, permit, "set_admission"
        )
        barrier = await self.kernel.set_execution_admission(
            context.grant, allowed, expected_revision=expected_revision, idempotency_key=control_id
        )
        return SetAdmissionResult(
            operation="set_admission",
            mutationReceipt=GrantMutationReceipt(
                operationId=control_id, snapshot=grant_snapshot(barrier, expected_incarnation)
            ),
        )

    async def validate_native_control(self, command, *, context_ref, expected_incarnation, permit):
        """Submit exact interrupt/interaction through the original Kernel path.

        The trusted caller supplies the original stable control command and its
        independent native permit. A cancel acknowledgement is never a terminal.
        """
        command = AgentControlCommand.model_validate(command)
        operation = {"interrupt": "cancel", "submit_interaction": "respond_interaction"}.get(
            command.command_type
        )
        if operation is None:
            raise ContextConflict("unsupported Teams native control")
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, operation)
        run_id = execution_grant_run_id(context.command)
        if (
            command.tenant_id != context.grant.tenant_id
            or command.agent_instance_id != context.grant.agent_instance_id
            or command.session_id != context.ref.sessionId
            or command.payload.get("run_id") != run_id
            or str(command.command_id) == context.ref.commandId
            or command.idempotency_key == context.ref.idempotencyKey
        ):
            raise ContextConflict("native control must target the exact original execution")
        run = await self.store.load_run(run_id)
        if (
            run is None
            or run.agent_instance_id != context.grant.agent_instance_id
            or run.session_id != context.ref.sessionId
            or run.metadata.get("command_id") != context.ref.commandId
        ):
            raise ContextConflict("native control original run is unavailable")
        if operation == "respond_interaction":
            interaction = await self.store.get(
                command.payload["interaction_id"],
                tenant_id=context.grant.tenant_id,
                agent_instance_id=context.grant.agent_instance_id,
                session_id=context.ref.sessionId,
                run_id=run_id,
            )
            if interaction is None:
                raise ContextConflict("interaction does not belong to the original execution")
        return command

    async def submit_native_control(
        self, command, *, context_ref, expected_incarnation, permit, native_permit
    ):
        command = await self.validate_native_control(
            command,
            context_ref=context_ref,
            expected_incarnation=expected_incarnation,
            permit=permit,
        )
        run_id = command.payload["run_id"]
        native_permit = await self.validate_native_authorization(
            native_permit,
            context_ref=context_ref,
            expected_incarnation=expected_incarnation,
        )
        receipt = await self.kernel.submit(
            command, permit=AgentControlPermit.model_validate(native_permit)
        )
        status = (
            receipt.status
            if receipt.status in {"accepted", "duplicate", "rejected"}
            else "uncertain"
        )
        return HostReceipt(
            status=status,
            commandId=str(command.command_id),
            idempotencyKey=command.idempotency_key,
            payloadDigest="sha256:" + command_digest(command),
            storeIncarnation=expected_incarnation,
            acceptedSeq=receipt.accepted_seq,
            runId=run_id if status in {"accepted", "duplicate"} else None,
        )

    async def resolve(self, ref: str, *, request) -> ExecutionPolicy:
        """Mount this resolver explicitly in the ManagedRuntime bootstrap.

        policy_factory must also wrap effectful tools with require_live_context;
        resolver invocation alone is not per-tool authorization.
        """
        if not self.capability_snapshot()["executionPolicy"]:
            raise ContextConflict("runtime policy enforcement is not mounted")
        context = await self.require_live_context(
            request.metadata.get("teams_context_ref"), request=request
        )
        if ref != context.policy_ref:
            raise ContextConflict("execution policy reference mismatch")
        policy = await self.policy_factory(context, request=request)
        if not isinstance(policy, ExecutionPolicy):
            raise TypeError("trusted policy factory must return ExecutionPolicy")
        return policy

    async def require_live_context(self, context_ref, *, request):
        context = await self._context(context_ref, await self.registry.incarnation())
        metadata = request.metadata
        if (
            request.session_id != context.ref.sessionId
            or request.user_id != context.owner_subject
            or metadata.get("command_id") != context.ref.commandId
            or metadata.get("run_id") != execution_grant_run_id(context.command)
            or metadata.get("execution_policy_ref") != context.policy_ref
        ):
            raise ContextConflict("runtime invocation does not match prepared execution")
        await self.kernel.require_execution_grant(context.grant)
        return context

    async def observe_execution(
        self,
        *,
        context_ref,
        expected_incarnation,
        permit,
        after_seq=0,
        limit=200,
        snapshot_upper_seq=None,
    ):
        """Page original canonical facts with a fixed session scan watermark.

        Child ancestry comes only from canonical source metadata. Unrelated
        runs consume cursor positions but are never disclosed. A bounded scan
        fails closed rather than pretending a partial scan is the final page.
        """
        if not 0 <= after_seq or not 1 <= limit <= 200:
            raise ContextConflict("invalid canonical event cursor")
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, "observe")
        receipt = await self._lookup(context, expected_incarnation)
        if receipt.status != "accepted":
            raise ContextConflict("original execution has no accepted canonical identity")
        rows, cursor = [], 0
        while len(rows) < 10000:
            page = await self.events.read(context.ref.sessionId, cursor, 200)
            if not page:
                break
            for envelope in page:
                if envelope.seq <= cursor:
                    raise ContextConflict("canonical event cursor did not advance")
                cursor = envelope.seq
                rows.append(envelope)
            if snapshot_upper_seq is not None and cursor >= snapshot_upper_seq:
                break
        else:
            raise ContextConflict("canonical snapshot scan budget exceeded")
        upper = cursor if snapshot_upper_seq is None else snapshot_upper_seq
        if not after_seq <= upper <= cursor:
            raise ContextConflict("canonical snapshot watermark is unavailable")
        authorized_runs = {receipt.runId}
        # Native children may publish before the parent annotation is seen.
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row.seq > upper or row.family != "runtime" or row.family_version != 2:
                    continue
                event = parse_runtime_event(row.payload)
                parent = event.source.metadata.get("parent_run_id")
                if parent in authorized_runs and event.run_id not in authorized_runs:
                    authorized_runs.add(event.run_id)
                    changed = True
        scanned = [row for row in rows if after_seq < row.seq <= upper][:limit]
        next_seq = scanned[-1].seq if scanned else upper
        items = []
        for row in scanned:
            if row.run_id not in authorized_runs:
                continue
            metadata = {}
            if row.family == "runtime" and row.family_version == 2:
                event = parse_runtime_event(row.payload)
                if event.run_id != row.run_id or event.seq != row.seq:
                    raise ContextConflict("canonical event identity mismatch")
                metadata = event.source.metadata
            item = CanonicalExecutionEvent(
                eventId=str(row.event_id),
                sessionId=row.session_id,
                runId=row.run_id,
                seq=row.seq,
                family=row.family,
                type=row.event_type,
                payload=row.payload,
                sourceRef=EventSourceRef(
                    nativeRunId=row.run_id,
                    parentRunId=metadata.get("parent_run_id"),
                    nativeEventType=metadata.get("native_event_type") or row.event_type,
                ),
            )
            items.append(item)
        if await self.registry.incarnation() != expected_incarnation:
            raise StoreIdentityMismatch("Kernel store identity changed during event read")
        return CanonicalEventBatch(
            sessionId=context.ref.sessionId,
            storeIncarnation=expected_incarnation,
            afterSeq=after_seq,
            nextSeq=next_seq,
            snapshotUpperSeq=upper,
            hasMore=next_seq < upper,
            items=items,
        )

    async def get_execution_effects(
        self, *, context_ref, expected_incarnation, permit, after="", limit=100
    ):
        """Read original durable evidence with recovery authority; never execute."""
        from ksadk.kernel.teams_tool_runtime import ExecutionEffectsPage

        parsed = TeamsExecutionPermit.model_validate(permit)
        if parsed.permitKind != "recovery":
            raise PermitError("effect recovery requires a read-only recovery permission")
        context, _ = await self._authorized(context_ref, expected_incarnation, parsed, "observe")
        journal = self.effect_journal
        if journal is None or journal.store_incarnation != expected_incarnation:
            raise StoreIdentityMismatch("original effect journal unavailable")
        receipt = await self._lookup(context, expected_incarnation)
        run = await self.store.load_run(receipt.runId) if receipt.runId else None
        if receipt.status != "accepted" or run is None:
            raise ContextConflict("original native Run unavailable for effects")
        rows = await journal.for_execution(context_ref, receipt.runId, after=after, limit=limit)
        for row in rows:
            expected = context.ref.model_dump(mode="json")
            actual = row.request.ref.model_dump(mode="json")
            for value in (expected, actual):
                value.pop("schedulerEpoch")
                value["nativeRunId"] = receipt.runId
            if (
                expected != actual
                or row.request.store_incarnation != expected_incarnation
                or row.request.journal_incarnation != journal.journal_incarnation
            ):
                raise ContextConflict("effect history scope mismatch")
        if await self.registry.incarnation() != expected_incarnation:
            raise StoreIdentityMismatch("original store changed while reading effects")
        return ExecutionEffectsPage(
            contextRef=context_ref,
            storeIncarnation=expected_incarnation,
            nativeRunId=receipt.runId,
            journalIncarnation=journal.journal_incarnation,
            items=rows,
            nextCursor=f"{rows[-1].request.effect_key}:{rows[-1].revision}"
            if len(rows) == limit
            else None,
        )

    async def get_execution_result(self, *, context_ref, expected_incarnation, permit):
        context, _ = await self._authorized(context_ref, expected_incarnation, permit, "get_result")
        receipt = await self._lookup(context, expected_incarnation)
        if receipt.status != "accepted":
            return {"status": "pending", "receipt": receipt}
        # Canonical terminal is a fixed upper bound. Do not infer result from
        # idle status, an arbitrary last message, child runs or transport EOF.
        after = 0
        items, phases, total_tokens = {}, {}, 0
        terminal = None
        scanned = 0
        while terminal is None and scanned < 10000:
            page = await self.events.read(context.ref.sessionId, after, 200)
            if not page:
                break
            scanned += len(page)
            for envelope in page:
                if envelope.seq <= after:
                    raise ContextConflict("canonical event cursor did not advance")
                after = envelope.seq
                if (
                    envelope.run_id != receipt.runId
                    or envelope.family != "runtime"
                    or envelope.family_version != 2
                ):
                    continue
                event = parse_runtime_event(envelope.payload)
                if event.event_type != envelope.event_type or event.seq != envelope.seq:
                    raise ContextConflict("canonical envelope/payload identity mismatch")
                if (
                    event.run_id != receipt.runId
                    or event.source.metadata.get("parent_run_id")
                    or event.parent_scope_id is not None
                ):
                    continue
                if event.event_type == "usage.reported":
                    total_tokens += event.total_tokens
                elif event.event_type == "item.started":
                    phases[(event.scope_id, event.item_id)] = event.phase
                elif event.event_type == "item.completed":
                    items[(event.scope_id, event.item_id)] = event
                elif event.event_type in _TERMINAL:
                    terminal = (envelope, event)
                    break
        if terminal is None:
            return {
                "status": "pending",
                "receipt": receipt,
                "reason": "scan_budget_exceeded"
                if scanned >= 10000
                else "canonical_terminal_pending",
            }
        envelope, event = terminal
        refs = getattr(event, "output_refs", ())
        selected = (
            [(ref.scope_id, ref.item_id) for ref in refs]
            if refs
            else [key for key in items if phases.get(key) == "final_answer"]
        )
        if any(key not in items for key in selected):
            return {
                "status": "pending",
                "reason": "canonical_output_unavailable",
                "receipt": receipt,
            }
        texts, artifacts = [], []
        for key in dict.fromkeys(selected):
            completed = items[key]
            parts = [ref.part_id for ref in refs if (ref.scope_id, ref.item_id) == key]
            if parts and None not in parts:
                snapshot_parts = tuple(
                    part for part in completed.snapshot.parts if part.part_id in parts
                )
                if set(parts) - {part.part_id for part in snapshot_parts}:
                    return {
                        "status": "pending",
                        "reason": "canonical_output_unavailable",
                        "receipt": receipt,
                    }
                completed = completed.model_copy(
                    update={
                        "snapshot": completed.snapshot.model_copy(update={"parts": snapshot_parts})
                    }
                )
            item = project_conversation_item(completed, session_id=context.ref.sessionId)
            if item.kind == "assistant_text":
                texts.append(str(item.payload.get("text") or ""))
            # Teams results carry immutable uploaded IDs. Conversation payloads
            # are display DTOs (and can contain URLs), not trusted artifact refs.
            # Inspect every selected part so mixed text/artifact messages do not
            # silently lose their produced objects.
            for part in completed.snapshot.parts:
                if part.content_type == "artifact":
                    reference = {"artifactId": part.artifact_id}
                    if reference not in artifacts:
                        artifacts.append(reference)
        candidate = {"result": "\n\n".join(texts), "artifacts": artifacts}
        evidence = TerminalEvidence(
            sessionId=context.ref.sessionId,
            runId=receipt.runId,
            commandId=context.ref.commandId,
            terminalSeq=envelope.seq,
            terminalEventDigest=digest(envelope.model_dump(mode="json")),
            resultDigest=digest(candidate),
        )
        if await self.registry.incarnation() != expected_incarnation:
            raise StoreIdentityMismatch("Kernel store identity changed during result read")
        return {
            "status": _TERMINAL[event.event_type],
            "candidate": candidate,
            "usage": {"totalTokens": total_tokens},
            "terminalEvidence": evidence,
            "receipt": receipt.model_copy(
                update={"nativeStatus": _TERMINAL[event.event_type], "terminalEvidence": evidence}
            ),
        }
