"""Authenticated Teams HTTP ports layered on the canonical AgentControl ingress.

The injected resolver selects only a preconfigured Host, never an endpoint from
request data. Every Host method verifies its own signed, operation-scoped permit;
loading a context or resolving a Host is not execution authorization.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.execution_grants import ExecutionGrantBlocked
from ksadk.kernel.teams_execution_context import ContextConflict, StoreIdentityMismatch
from ksadk.plugins.teams.cloud_permits import BindingProbePermit, PermitError, TeamsExecutionPermit
from ksadk.plugins.teams.effect_contracts import EffectsRequest as EffectsRequest

TEAMS_HOST_BASE_PATH = "/agent-kernel/teams-host/v1"


class HostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contextRef: str = Field(min_length=1)
    expectedIncarnation: str = Field(min_length=1)
    permit: TeamsExecutionPermit

    def arguments(self):
        return dict(
            context_ref=self.contextRef,
            expected_incarnation=self.expectedIncarnation,
            permit=self.permit,
        )


class DescribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permit: BindingProbePermit


class LookupRequest(HostRequest):
    commandId: str
    idempotencyKey: str
    payloadDigest: str


class ControlLookupRequest(HostRequest):
    command: AgentControlCommand


class GetGrantRequest(HostRequest):
    renewalId: str | None = None


class SetGrantRequest(HostRequest):
    state: Literal["active", "suspended", "revoked"]
    expectedRevision: int = Field(ge=1)
    controlId: str | None = None
    renewalId: str | None = None
    expiresAt: str | None = None


class SetAdmissionRequest(HostRequest):
    admissionAllowed: bool = Field(strict=True)
    expectedAdmissionRevision: int = Field(ge=1)
    controlId: str = Field(min_length=1)


class ObserveRequest(HostRequest):
    afterSeq: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=200)
    snapshotUpperSeq: int | None = Field(default=None, ge=0)


def host_error(error: Exception) -> JSONResponse:
    if isinstance(error, StoreIdentityMismatch):
        status, code = 409, "store_identity_mismatch"
    elif isinstance(error, (PermitError, ExecutionGrantBlocked)):
        status, code = 403, "teams_authorization_rejected"
    elif isinstance(error, ContextConflict):
        status, code = 409, "teams_context_conflict"
    else:
        status, code = 503, "teams_host_unavailable"
    # Never expose callback URL, credential, SQL or context contents.
    return JSONResponse({"error": {"Code": code, "Message": code}}, status_code=status)


def create_teams_host_router(resolve_host):
    """resolve_host(*, context_ref, permit) -> an actual KernelTeamsExecutionHost.

    Probe selection uses a signed binding reference. Execution selection uses a
    durable or Server-loaded context reference; the Host then fences every scope.
    Internal network/service authentication should additionally wrap this router.
    """
    router = APIRouter(prefix=TEAMS_HOST_BASE_PATH)

    async def invoke(body, operation, **extra):
        try:
            host = await resolve_host(context_ref=body.contextRef, permit=body.permit)
            result = await getattr(host, operation)(**body.arguments(), **extra)
            return JSONResponse(jsonable_encoder(result))
        except (ValueError, ExecutionGrantBlocked) as error:
            return host_error(error)

    @router.post("/DescribeBinding")
    async def describe(body: DescribeRequest):
        try:
            host = await resolve_host(context_ref=None, permit=body.permit)
            return JSONResponse(jsonable_encoder(await host.describe_binding(permit=body.permit)))
        except (ValueError, ExecutionGrantBlocked) as error:
            return host_error(error)

    @router.post("/PrepareExecution")
    async def prepare(body: HostRequest):
        return await invoke(body, "prepare_execution")

    @router.post("/EnsureSession")
    async def ensure(body: HostRequest):
        return await invoke(body, "ensure_session")

    @router.post("/LookupExecution")
    async def lookup(body: LookupRequest):
        return await invoke(
            body,
            "lookup_execution",
            command_id=body.commandId,
            idempotency_key=body.idempotencyKey,
            payload_digest=body.payloadDigest,
        )

    @router.post("/GetExecutionEffects")
    async def effects(body: EffectsRequest):
        return await invoke(body, "get_execution_effects", after=body.after, limit=body.limit)

    @router.post("/GetExecutionResult")
    async def result(body: HostRequest):
        return await invoke(body, "get_execution_result")

    @router.post("/LookupControlExecution")
    async def lookup_control(body: ControlLookupRequest):
        return await invoke(body, "lookup_control", command=body.command)

    @router.post("/GetExecutionGrant")
    async def get_grant(body: GetGrantRequest):
        return await invoke(body, "get_execution_grant", renewal_id=body.renewalId)

    @router.post("/SetExecutionGrant")
    async def set_grant(body: SetGrantRequest):
        if body.renewalId is not None:
            try:
                if body.controlId is not None or body.state != "active" or body.expiresAt is None:
                    raise ContextConflict("invalid grant renewal")
                host = await resolve_host(context_ref=body.contextRef, permit=body.permit)
                # Time authority comes from the authenticated Server loader, not
                # a node's wall clock or a timestamp in this request body.
                if host.renewal_loader is None:
                    raise ContextConflict("trusted renewal operation loader unavailable")
                window = await host.renewal_loader(body.contextRef, body.renewalId)
                if window.expires_at != body.expiresAt:
                    raise ContextConflict("renewal differs from trusted Server grant window")
                value = await host.renew_execution_grant(
                    **body.arguments(),
                    expected_revision=body.expectedRevision,
                    renewal_id=body.renewalId,
                    trusted_window=window,
                )
                return JSONResponse(jsonable_encoder(value))
            except (ValueError, ExecutionGrantBlocked) as error:
                return host_error(error)
        if body.controlId is None or body.expiresAt is not None:
            return host_error(ContextConflict("invalid grant mutation"))
        return await invoke(
            body,
            "set_execution_grant",
            state=body.state,
            expected_revision=body.expectedRevision,
            control_id=body.controlId,
        )

    @router.post("/SetExecutionAdmission")
    async def set_admission(body: SetAdmissionRequest):
        return await invoke(
            body,
            "set_execution_admission",
            allowed=body.admissionAllowed,
            expected_revision=body.expectedAdmissionRevision,
            control_id=body.controlId,
        )

    @router.post("/ObserveExecution")
    async def observe(body: ObserveRequest):
        return await invoke(
            body,
            "observe_execution",
            after_seq=body.afterSeq,
            limit=body.limit,
            snapshot_upper_seq=body.snapshotUpperSeq,
        )

    return router


class TeamsNativeIngressGate:
    """Require dual permits for Teams commands, including native endpoint calls."""

    def __init__(self, resolve_host, owns_session):
        self.resolve_host, self.owns_session = resolve_host, owns_session

    async def __call__(self, *, command, teams: Any, kernel, native_permit):
        is_teams = bool(command.payload.get("teams_context_ref")) or await self.owns_session(
            command.session_id
        )
        if teams is None:
            if is_teams:
                raise PermitError("Teams execution requires its own signed authorization")
            return
        body = HostRequest.model_validate(teams)
        host = await self.resolve_host(context_ref=body.contextRef, permit=body.permit)
        if host.kernel is not kernel:
            raise ContextConflict("Teams Host must own the native ingress Kernel")
        await host.validate_native_authorization(
            native_permit,
            context_ref=body.contextRef,
            expected_incarnation=body.expectedIncarnation,
        )
        if command.command_type == "enqueue":
            if command.payload.get("teams_context_ref") != body.contextRef:
                raise ContextConflict("native command context mismatch")
            await host.validate_enqueue(
                command, expected_incarnation=body.expectedIncarnation, permit=body.permit
            )
        else:
            await host.validate_native_control(command, **body.arguments())
