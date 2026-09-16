from __future__ import annotations

import pytest

from ksadk.kernel.worker_identity import prepare_worker_identity
from ksadk.conversations.runtime_persistence import ensure_conversation_session
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.invocation_identity import identity_native_user_id
from tests.kernel.control_harness import command


@pytest.mark.asyncio
async def test_worker_uses_persisted_public_agent_id_for_hosted_instance_alias():
    service = InMemorySessionService()
    invocation_identity = {
        "identity_namespace": "customer-crm",
        "tenant_id": "tenant-a",
        "subject_type": "user",
        "subject_id": "user-7",
    }
    await service.create_session(
        "ar-cloud-agent",
        identity_native_user_id(invocation_identity),
        session_id="cloud-session",
    )
    admitted = command(session_id="cloud-session").model_copy(
        update={
            "tenant_id": "tenant-a",
            "payload": {"content": {"text": "hello"}, "invocation_identity": invocation_identity},
        }
    )

    user_id, retained_identity = await prepare_worker_identity(
        command=admitted,
        defaults={"agent_id": "ai-instance-alias"},
        session_service=service,
    )

    assert user_id == identity_native_user_id(invocation_identity)
    assert retained_identity == invocation_identity


@pytest.mark.asyncio
async def test_worker_adopts_existing_session_when_runtime_uses_instance_alias():
    service = InMemorySessionService()
    identity = {
        "identity_namespace": "customer-crm",
        "tenant_id": "tenant-a",
        "subject_type": "user",
        "subject_id": "user-7",
    }
    await service.create_session(
        "ar-cloud-agent", identity_native_user_id(identity), session_id="cloud-session"
    )

    session = await ensure_conversation_session(
        agent_id="ai-instance-alias",
        user_id="agent-kernel",
        session_id="cloud-session",
        session_service_provider=lambda: service,
        invocation_identity=identity,
        allow_agent_alias=True,
    )

    assert session.agent_id == "ar-cloud-agent"
    assert session.user_id == identity_native_user_id(identity)
