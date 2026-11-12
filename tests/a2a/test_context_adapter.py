from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.a2a.context_store import SQLiteA2AContextStore
from ksadk.a2a.identity import A2AIngressIdentity
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.runtime import RunHandle


class _RecordingRuntimeAdapter:
    def __init__(self) -> None:
        self.requests = []

    async def start(self, request):  # noqa: ANN001
        self.requests.append(request)
        return RunHandle(
            run_id="run-1",
            session_id=request.session_id,
            runtime_type="test",
        )


def _context(external_context_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        context_id=external_context_id,
        current_task=None,
        metadata={},
        call_context=SimpleNamespace(
            tenant="account-a/tenant-a/user/caller-a",
            state={
                "a2a_identity": A2AIngressIdentity(
                    account_id="account-a",
                    tenant_id="tenant-a",
                    caller_principal_type="user",
                    caller_principal_id="caller-a",
                    target_agent_id="ar-target",
                    target_runtime_id="runtime-a",
                    target_a2a_agent_id="a2a-agent-00000000000040008000000000000041",
                    authn_mode="api_key",
                )
            },
        ),
    )


@pytest.mark.asyncio
async def test_task_adapter_uses_owner_scoped_internal_session_id(tmp_path) -> None:
    runtime = _RecordingRuntimeAdapter()
    adapter = A2ARuntimeTaskAdapter(
        runtime,  # type: ignore[arg-type]
        runtime_type="test",
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
    )

    first_context = _context("external-context")
    await adapter.start_task(task_id="task-1", context=first_context, input_data="hello")
    second_context = _context("external-context")
    await adapter.start_task(task_id="task-2", context=second_context, input_data="again")

    first_session = runtime.requests[0].session_id
    assert first_session != "external-context"
    assert runtime.requests[1].session_id == first_session


@pytest.mark.asyncio
async def test_existing_task_with_missing_context_mapping_fails_closed(tmp_path) -> None:
    runtime = _RecordingRuntimeAdapter()
    adapter = A2ARuntimeTaskAdapter(
        runtime,  # type: ignore[arg-type]
        runtime_type="test",
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
    )
    context = _context("external-context")
    context.current_task = SimpleNamespace(
        id="existing-task",
        context_id="external-context",
    )

    with pytest.raises(RuntimeError, match="A2A_CONTEXT_MAPPING_NOT_FOUND"):
        await adapter.prepare_context(context)


@pytest.mark.asyncio
async def test_start_creates_checkpoint_session_before_runtime_start(tmp_path) -> None:
    from ksadk.sessions import InMemorySessionService, bind_session_service

    service = InMemorySessionService()

    class CheckpointRuntime(_RecordingRuntimeAdapter):
        async def start(self, request):
            session = await service.get_session_metadata(request.session_id)
            assert session is not None
            assert session.agent_id == "ar-target"
            assert session.user_id == "account-a/tenant-a/user/caller-a"
            return await super().start(request)

    runtime = CheckpointRuntime()
    adapter = A2ARuntimeTaskAdapter(
        runtime,
        runtime_type="test",
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
    )
    context = _context("external-context")
    context.metadata = {"agent_id": "forged-agent", "user_id": "forged-user"}
    with bind_session_service(service):
        await adapter.start_task(task_id="task-1", context=context, input_data="hello")
    assert "agent_id" not in runtime.requests[0].metadata
    assert "user_id" not in runtime.requests[0].metadata


@pytest.mark.asyncio
async def test_start_reuses_mapped_session_with_deployment_owner_alias(tmp_path) -> None:
    from ksadk.sessions import InMemorySessionService, bind_session_service

    service = InMemorySessionService()
    runtime = _RecordingRuntimeAdapter()
    adapter = A2ARuntimeTaskAdapter(
        runtime,
        runtime_type="test",
        context_store=SQLiteA2AContextStore(tmp_path / "contexts.sqlite3"),
    )
    context = _context("external-context")
    session_id = await adapter.prepare_context(context)
    original = await service.create_session("deployment-agent", context.call_context.tenant, session_id=session_id)
    with bind_session_service(service):
        await adapter.start_task(task_id="task-1", context=context, input_data="hello")
        await adapter.start_task(task_id="task-2", context=_context("external-context"), input_data="again")
    stored = await service.get_session_metadata(session_id)
    assert stored.agent_id == original.agent_id
    assert stored.created_at == original.created_at
    assert [request.session_id for request in runtime.requests] == [session_id, session_id]
