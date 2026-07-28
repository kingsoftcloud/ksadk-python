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
