from __future__ import annotations

import pytest
from fastapi import HTTPException

from ksadk.conversations.runtime_persistence import ensure_conversation_session
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.invocation_identity import (
    SESSION_IDENTITY_SCOPE,
    identity_native_user_id,
    identity_scope_ref,
    identity_session_is_legacy,
)


def _identity(tenant_id: str, subject_id: str = "user-7") -> PlatformIdentityContext:
    return PlatformIdentityContext(
        identity_namespace="customer-crm",
        tenant_id=tenant_id,
        subject_type="user",
        subject_id=subject_id,
    )


def _iam_user_identity(user_id: str) -> PlatformIdentityContext:
    return PlatformIdentityContext(
        identity_namespace="kscloud-iam",
        tenant_id="account-1",
        subject_type="user",
        subject_id=user_id,
    )


def test_native_identity_keys_include_the_full_business_scope() -> None:
    tenant_a = _identity("tenant-a")
    tenant_b = _identity("tenant-b")

    assert identity_native_user_id(tenant_a).startswith("aeu_")
    assert identity_scope_ref(tenant_a).startswith("aei_")
    assert identity_native_user_id(tenant_a) != identity_native_user_id(tenant_b)
    assert identity_native_user_id(tenant_a) == identity_native_user_id(_identity("tenant-a"))


def test_only_noncanonical_authorized_user_keys_are_legacy() -> None:
    identity = _identity("tenant-a")

    assert not identity_session_is_legacy(identity, identity_native_user_id(identity))
    assert identity_session_is_legacy(identity, "legacy-user")


@pytest.mark.asyncio
async def test_identity_aware_session_is_bound_before_use() -> None:
    service = InMemorySessionService()
    identity = _identity("tenant-a")

    session = await ensure_conversation_session(
        agent_id="agent-1",
        user_id="bff-service",
        session_id="session-1",
        session_service_provider=lambda: service,
        invocation_identity=identity,
    )

    assert session.user_id == identity_native_user_id(identity)
    binding = await service.get_state(
        "agent-1", session.user_id, session.id, SESSION_IDENTITY_SCOPE
    )
    assert binding is not None
    assert binding.state["owner"] == {
        "identity_namespace": "customer-crm",
        "tenant_id": "tenant-a",
        "subject_type": "user",
        "subject_id": "user-7",
    }
    assert binding.state["native_user_id"] == session.user_id


@pytest.mark.asyncio
async def test_same_session_id_is_hidden_from_another_business_identity() -> None:
    service = InMemorySessionService()
    await ensure_conversation_session(
        agent_id="agent-1",
        user_id="service",
        session_id="shared-looking-id",
        session_service_provider=lambda: service,
        invocation_identity=_identity("tenant-a"),
    )

    with pytest.raises(HTTPException) as caught:
        await ensure_conversation_session(
            agent_id="agent-1",
            user_id="service",
            session_id="shared-looking-id",
            session_service_provider=lambda: service,
            invocation_identity=_identity("tenant-b"),
        )

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_verified_iam_user_legacy_adoption_preserves_history_user_id() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "legacy-user", "legacy-session")

    session = await ensure_conversation_session(
        agent_id="agent-1",
        user_id="legacy-user",
        session_id="legacy-session",
        session_service_provider=lambda: service,
        invocation_identity=_iam_user_identity("legacy-user"),
    )

    assert session.user_id == "legacy-user"
    rebound = await ensure_conversation_session(
        agent_id="agent-1",
        user_id="a-new-public-hint",
        session_id="legacy-session",
        session_service_provider=lambda: service,
        invocation_identity=_iam_user_identity("legacy-user"),
    )
    assert rebound.user_id == "legacy-user"

    with pytest.raises(HTTPException) as caught:
        await ensure_conversation_session(
            agent_id="agent-1",
            user_id="legacy-user",
            session_id="legacy-session",
            session_service_provider=lambda: service,
            invocation_identity=_iam_user_identity("different-user"),
        )
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_custom_identity_cannot_first_claim_an_unowned_legacy_session() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "same-business-user", "legacy-session")

    with pytest.raises(HTTPException) as caught:
        await ensure_conversation_session(
            agent_id="agent-1",
            user_id="same-business-user",
            session_id="legacy-session",
            session_service_provider=lambda: service,
            invocation_identity=_identity("tenant-a", "same-business-user"),
        )

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_verified_iam_account_can_adopt_any_legacy_user_for_its_agent() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "old-web-user", "legacy-session")
    account_identity = PlatformIdentityContext(
        identity_namespace="kscloud-iam",
        tenant_id="account-1",
        subject_type="account",
        subject_id="account-1",
    )

    session = await ensure_conversation_session(
        agent_id="agent-1",
        user_id="",
        session_id="legacy-session",
        session_service_provider=lambda: service,
        invocation_identity=account_identity,
    )

    assert session.user_id == "old-web-user"
    binding = await service.get_state(
        "agent-1", "old-web-user", "legacy-session", SESSION_IDENTITY_SCOPE
    )
    assert binding is not None
    assert binding.state["owner"]["subject_type"] == "account"


@pytest.mark.asyncio
async def test_iam_account_cannot_claim_an_unbound_identity_aware_row() -> None:
    service = InMemorySessionService()
    child_identity = _iam_user_identity("child-user")
    account_identity = PlatformIdentityContext(
        identity_namespace="kscloud-iam",
        tenant_id="account-1",
        subject_type="account",
        subject_id="account-1",
    )
    await service.create_session(
        "agent-1",
        identity_native_user_id(child_identity),
        "concurrent-session",
    )

    with pytest.raises(HTTPException) as caught:
        await ensure_conversation_session(
            agent_id="agent-1",
            user_id="caller-value",
            session_id="concurrent-session",
            session_service_provider=lambda: service,
            invocation_identity=account_identity,
        )

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_incomplete_private_identity_fails_closed() -> None:
    service = InMemorySessionService()
    incomplete = PlatformIdentityContext(
        identity_namespace="customer-crm",
        tenant_id="tenant-a",
    )

    with pytest.raises(HTTPException) as caught:
        await ensure_conversation_session(
            agent_id="agent-1",
            user_id="service",
            session_id="session-1",
            session_service_provider=lambda: service,
            invocation_identity=incomplete,
        )
    assert caught.value.status_code == 401
