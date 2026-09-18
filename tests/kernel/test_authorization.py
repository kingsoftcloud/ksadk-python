# -*- coding: utf-8 -*-
"""AgentControlPermitVerifier：Ed25519 permit 验证（Kernel Task 6 Step 4）。"""

from __future__ import annotations

import pytest

from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    PermitExpiredError,
    VerifiedAdmission,
    canonical_permit_bytes,
)
from ksadk.kernel.contracts import AgentStatusQuery
from ksadk.kernel.errors import AgentKernelError, InvalidPermitError
from tests.kernel.control_harness import (
    AGENT,
    CLOCK_AT,
    EXPIRED_AT,
    PERMIT_REF,
    TENANT,
    PermitAuthority,
    command,
    kernel_stack,
)


def status_query(session_id: str | None = "s1") -> AgentStatusQuery:
    return AgentStatusQuery(
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        authorization_ref=PERMIT_REF,
        session_id=session_id,
    )


async def test_valid_permit_verifies_and_returns_admission():
    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    cmd = command()
    admission = await stack.verifier.verify(permit, cmd, "enqueue", CLOCK_AT)
    assert isinstance(admission, VerifiedAdmission)
    assert admission.permit_id == permit.permit_id
    assert admission.subject_ref == permit.subject_ref
    assert admission.claims_digest == permit.claims_digest
    assert admission.operation == "enqueue"


async def test_tampered_signature_fails_closed():
    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    tampered = permit.model_copy(update={"signature": permit.signature[:-1]})
    with pytest.raises(InvalidPermitError):
        await stack.verifier.verify(tampered, command(), "enqueue", CLOCK_AT)


async def test_operation_not_allowed_is_rejected():
    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(permit, command("interrupt"), "interrupt", CLOCK_AT)
    assert "operation_not_allowed" in str(error.value)


async def test_resource_binding_mismatch_is_rejected():
    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    with pytest.raises(InvalidPermitError):
        await stack.verifier.verify(permit, command(tenant_id="tenant-2"), "enqueue", CLOCK_AT)
    with pytest.raises(InvalidPermitError):
        await stack.verifier.verify(permit, status_query("s2"), "get_status", CLOCK_AT)


async def test_session_binding_mismatch_is_rejected():
    stack = await kernel_stack()
    permit = stack.permit("get_status")
    with pytest.raises(InvalidPermitError):
        await stack.verifier.verify(permit, status_query("s9"), "get_status", CLOCK_AT)


async def test_unknown_key_fails_closed_after_one_refresh():
    stack = await kernel_stack()
    other = PermitAuthority(key_id="key-other")
    permit = other.permit(operations=("enqueue",), session_id="s1")
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(permit, command(), "enqueue", CLOCK_AT)
    assert "unknown_signing_key" in str(error.value)
    assert stack.jwks.fetch_count == 1


async def test_jwks_cache_hits_and_max_age_refresh():
    stack = await kernel_stack(verifier_cache_max_age=100.0)
    permit = stack.permit("enqueue", nonce="nonce-cache")
    cmd = command(idempotency_key="cache-1")
    cmd2 = command(idempotency_key="cache-2")
    # 不同 nonce 的两张 permit，同一 key 只应 fetch 一次。
    await stack.verifier.verify(permit, cmd, "enqueue", CLOCK_AT)
    permit2 = stack.permit("enqueue", nonce="nonce-cache-2")
    await stack.verifier.verify(permit2, cmd2, "enqueue", CLOCK_AT)
    assert stack.jwks.fetch_count == 1


async def test_nonce_reuse_only_for_same_command_retry():
    stack = await kernel_stack()
    permit = stack.permit("enqueue", nonce="nonce-1")
    cmd = command(idempotency_key="same")
    await stack.verifier.verify(permit, cmd, "enqueue", CLOCK_AT)
    # 完全相同的 command/idempotency 网络重试可通过。
    await stack.verifier.verify(permit, cmd, "enqueue", CLOCK_AT)
    # 同 nonce 被另一 request 复用则拒绝。
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(
            permit, command(idempotency_key="other"), "enqueue", CLOCK_AT
        )
    assert "nonce_reuse" in str(error.value)


async def test_expired_permit_raises_distinct_error():
    stack = await kernel_stack()
    permit = stack.permit("enqueue", expires_at=EXPIRED_AT)
    with pytest.raises(PermitExpiredError):
        await stack.verifier.verify(permit, command(), "enqueue", CLOCK_AT)


async def test_canonical_permit_bytes_is_key_sorted_no_whitespace():
    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    raw = canonical_permit_bytes(permit)
    text = raw.decode("utf-8")
    assert "signature" not in permit.model_dump(exclude={"signature"})
    assert ": " not in text and ", " not in text
    assert text.index('"alg"') < text.index('"allowed_operations"') < text.index('"expires_at"')


def test_expired_error_is_invalid_permit_code():
    error = PermitExpiredError("permit_expired")
    assert isinstance(error, AgentKernelError)
    assert error.code == "invalid_permit"


# ------------------------------------------- review P0: permit binding / TTL


async def test_forged_authorization_ref_is_rejected():
    """P0-1: request.authorization_ref 必须等于 permit.permit_id。"""

    stack = await kernel_stack()
    permit = stack.permit("enqueue")
    forged = command(authorization_ref="permit-forged")
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(permit, forged, "enqueue", CLOCK_AT)
    assert "authorization_ref" in str(error.value)


async def test_permit_not_yet_valid_is_rejected():
    """P0-2a: issued_at 在 now 之后（时间倒签）必须拒绝。"""

    stack = await kernel_stack()
    permit = stack.permit("enqueue", issued_at="2026-08-18T00:04:00Z")
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(permit, command(), "enqueue", CLOCK_AT)
    assert "not_yet_valid" in str(error.value)


async def test_permit_ttl_exceeds_maximum_is_rejected():
    """P0-2b: expires_at - issued_at > 300s 必须拒绝。"""

    stack = await kernel_stack()
    permit = stack.permit("enqueue", expires_at="2026-08-18T00:06:00Z")
    with pytest.raises(InvalidPermitError) as error:
        await stack.verifier.verify(permit, command(), "enqueue", CLOCK_AT)
    assert "ttl" in str(error.value)


async def test_session_bound_permit_cannot_scope_up_to_instance_query():
    """P0-3: session-bound permit 不能用于 session_id=None 的 instance 级查询。"""

    stack = await kernel_stack()
    permit = stack.permit("get_status", session_id="s1")
    with pytest.raises(InvalidPermitError):
        await stack.verifier.verify(permit, status_query(None), "get_status", CLOCK_AT)
    # 显式匹配 session 的查询仍通过。
    admission = await stack.verifier.verify(
        permit, status_query("s1"), "get_status", CLOCK_AT
    )
    assert admission.permit_id == permit.permit_id


# ----------------------------------------------- review P0: durable nonce store


async def test_nonce_reuse_detected_across_verifiers_via_shared_nonce_store():
    """P0-4: nonce 去重必须走注入的 nonce store（跨 Pod / 重启 durable）。"""

    from ksadk.kernel.authorization import InMemoryNonceStore

    stack = await kernel_stack()
    store = InMemoryNonceStore()
    verifier_a = AgentControlPermitVerifier(
        stack.jwks, nonce_store=store, cache_max_age_seconds=300.0
    )
    verifier_b = AgentControlPermitVerifier(
        stack.jwks, nonce_store=store, cache_max_age_seconds=300.0
    )
    permit = stack.permit("enqueue", nonce="shared-nonce")
    await verifier_a.verify(permit, command(), "enqueue", CLOCK_AT)
    # 另一个进程/实例的 verifier 共享同一 durable store 时必须拒绝重放。
    with pytest.raises(InvalidPermitError) as error:
        await verifier_b.verify(
            permit, command(idempotency_key="replay"), "enqueue", CLOCK_AT
        )
    assert "nonce_reuse" in str(error.value)
