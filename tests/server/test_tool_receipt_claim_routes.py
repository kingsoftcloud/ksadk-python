from __future__ import annotations

import pytest
from fastapi import HTTPException

from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.routes import dependencies
from ksadk.server.routes.models import (
    GetToolReceiptClaimActionRequest,
    ReconcileToolReceiptClaimActionRequest,
)
from ksadk.server.routes.sessions import (
    get_tool_receipt_claim_action,
    reconcile_tool_receipt_claim_action,
)
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.mark.asyncio
async def test_get_and_reconcile_unknown_tool_receipt_claim(monkeypatch) -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    claim = await service.claim_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:route",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id="claim-1",
    )
    monkeypatch.setattr(dependencies, "resolve_session_service", lambda: service)
    identity = PlatformIdentityContext()

    missing = await get_tool_receipt_claim_action(
        GetToolReceiptClaimActionRequest(
            AgentId="agent-1", SessionId="session-1", IdempotencyKey="tool_receipt:missing"
        ),
        identity,
    )
    assert missing["Data"]["Found"] is False

    current = await get_tool_receipt_claim_action(
        GetToolReceiptClaimActionRequest(
            AgentId="agent-1", SessionId="session-1", IdempotencyKey=claim.idempotency_key
        ),
        identity,
    )
    assert current["Data"]["ToolReceiptClaim"]["State"] == "unknown"
    assert current["Data"]["ToolReceiptClaim"]["ReconciliationRequired"] is True

    reconciled = await reconcile_tool_receipt_claim_action(
        ReconcileToolReceiptClaimActionRequest(
            AgentId="agent-1",
            SessionId="session-1",
            IdempotencyKey=claim.idempotency_key,
            ClaimId=claim.claim_id,
            Reason="operator confirmed the external system did not apply the write",
        ),
        identity,
    )
    assert reconciled["Data"]["ToolReceiptClaim"]["State"] == "failed"
    assert reconciled["Data"]["ToolReceiptClaim"]["ReconciliationRequired"] is False

    with pytest.raises(HTTPException) as exc_info:
        await reconcile_tool_receipt_claim_action(
            ReconcileToolReceiptClaimActionRequest(
                AgentId="agent-1",
                SessionId="session-1",
                IdempotencyKey=claim.idempotency_key,
                ClaimId=claim.claim_id,
                Reason="second attempt",
            ),
            identity,
        )
    assert exc_info.value.status_code == 409

    unknown = await service.claim_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:route-race",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id="claim-race",
    )
    original_settle = service.settle_tool_receipt

    async def changed_concurrently(*args, **kwargs):
        raise ValueError("terminal result conflict")

    monkeypatch.setattr(service, "settle_tool_receipt", changed_concurrently)
    with pytest.raises(HTTPException) as race_error:
        await reconcile_tool_receipt_claim_action(
            ReconcileToolReceiptClaimActionRequest(
                AgentId="agent-1",
                SessionId="session-1",
                IdempotencyKey=unknown.idempotency_key,
                ClaimId=unknown.claim_id,
                Reason="race",
            ),
            identity,
        )
    assert race_error.value.status_code == 409
    monkeypatch.setattr(service, "settle_tool_receipt", original_settle)
