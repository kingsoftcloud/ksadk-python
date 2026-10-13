from __future__ import annotations

import asyncio

import pytest

from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService


@pytest.mark.asyncio
async def test_local_receipt_claim_is_atomic_across_service_instances(tmp_path) -> None:
    path = tmp_path / "sessions.sqlite"
    first = LocalSessionService(path)
    second = LocalSessionService(path)
    await first.create_session("agent-1", "user-1", session_id="session-1")

    async def claim(service, claim_id):
        return await service.claim_tool_receipt(
            "session-1",
            idempotency_key="tool_receipt:atomic",
            tool_name="write_workspace_file",
            arguments_digest="args-digest",
            claim_id=claim_id,
        )

    claims = await asyncio.gather(claim(first, "claim-1"), claim(second, "claim-2"))
    assert sum(item.acquired for item in claims) == 1
    assert {item.state for item in claims} == {"unknown"}

    winner = next(item for item in claims if item.acquired)
    settled = await first.settle_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:atomic",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id=winner.claim_id,
        state="completed",
        output={"ok": True},
    )
    assert settled.state == "completed"
    replay = await second.claim_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:atomic",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id="claim-3",
    )
    assert not replay.acquired
    assert replay.state == "completed"
    assert replay.output == {"ok": True}

    with pytest.raises(ValueError, match="idempotency key conflict"):
        await second.claim_tool_receipt(
            "session-1",
            idempotency_key="tool_receipt:atomic",
            tool_name="write_workspace_file",
            arguments_digest="different-args",
            claim_id="claim-collision",
        )


@pytest.mark.asyncio
async def test_unknown_claim_blocks_session_delete_but_terminal_claim_allows_cleanup() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    claim = await service.claim_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:delete",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id="claim-1",
    )
    with pytest.raises(ValueError, match="durable tool receipt claims"):
        await service.delete_session("session-1")

    await service.settle_tool_receipt(
        "session-1",
        idempotency_key=claim.idempotency_key,
        tool_name=claim.tool_name,
        arguments_digest=claim.arguments_digest,
        claim_id=claim.claim_id,
        state="completed",
        output={"ok": True},
    )
    assert await service.delete_session("session-1")
    assert await service.create_session("agent-1", "user-1", session_id="session-1")


@pytest.mark.asyncio
async def test_corrupt_claim_state_fails_closed() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    claim = await service.claim_tool_receipt(
        "session-1",
        idempotency_key="tool_receipt:corrupt",
        tool_name="write_workspace_file",
        arguments_digest="args-digest",
        claim_id="claim-1",
    )
    service._tool_receipts[("session-1", claim.idempotency_key)] = claim.__class__(
        **{**claim.__dict__, "state": "bogus", "acquired": False}
    )
    with pytest.raises(ValueError):
        await service.claim_tool_receipt(
            "session-1",
            idempotency_key=claim.idempotency_key,
            tool_name=claim.tool_name,
            arguments_digest=claim.arguments_digest,
            claim_id="claim-2",
        )
