"""Authenticated durable tool receipt claim and reconciliation routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ksadk.runtime_context import PlatformIdentityContext

from ..invocation_identity import resolve_trusted_invocation_identity
from . import dependencies as deps
from .common import _action_response
from .models import GetToolReceiptClaimActionRequest, ReconcileToolReceiptClaimActionRequest
from .routers import tools_router


@tools_router.post("/agentengine/api/v1/GetToolReceiptClaim")
async def get_tool_receipt_claim_action(
    request: GetToolReceiptClaimActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    from .sessions import _require_identity_action_session

    service = deps.resolve_session_service()
    await _require_identity_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
    )
    claim = await service.get_tool_receipt_claim(
        request.SessionId,
        idempotency_key=str(request.IdempotencyKey).strip(),
    )
    payload = None
    if claim is not None:
        payload = {
            "SessionId": claim.session_id,
            "IdempotencyKey": claim.idempotency_key,
            "ToolName": claim.tool_name,
            "ArgumentsDigest": claim.arguments_digest,
            "State": claim.state,
            "ClaimId": claim.claim_id,
            "Output": claim.output,
            "ReconciliationRequired": claim.state == "unknown",
        }
    return _action_response(
        "GetToolReceiptClaim",
        {"ToolReceiptClaim": payload, "Found": claim is not None},
    )


@tools_router.post("/agentengine/api/v1/ReconcileToolReceiptClaim")
async def reconcile_tool_receipt_claim_action(
    request: ReconcileToolReceiptClaimActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    from .sessions import _require_identity_action_session

    service = deps.resolve_session_service()
    await _require_identity_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
    )
    claim = await service.get_tool_receipt_claim(
        request.SessionId,
        idempotency_key=str(request.IdempotencyKey).strip(),
    )
    if claim is None:
        raise HTTPException(status_code=404, detail="Tool receipt claim not found")
    if claim.state != "unknown" or claim.claim_id != str(request.ClaimId).strip():
        raise HTTPException(status_code=409, detail="Tool receipt claim is not reconcilable")
    reason = str(request.Reason).strip()
    try:
        settled = await service.settle_tool_receipt(
            request.SessionId,
            idempotency_key=claim.idempotency_key,
            tool_name=claim.tool_name,
            arguments_digest=claim.arguments_digest,
            claim_id=claim.claim_id,
            state="failed",
            output={
                "ok": False,
                "error_type": "ToolReceiptReconciled",
                "error_message": reason,
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Tool receipt claim changed concurrently"
        ) from exc
    return _action_response(
        "ReconcileToolReceiptClaim",
        {
            "ToolReceiptClaim": {
                "SessionId": settled.session_id,
                "IdempotencyKey": settled.idempotency_key,
                "State": settled.state,
                "ClaimId": settled.claim_id,
                "Output": settled.output,
                "ReconciliationRequired": False,
            }
        },
    )
