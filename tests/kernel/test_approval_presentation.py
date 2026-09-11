import json
from types import SimpleNamespace

import pytest

from ksadk.events.canonical import ApprovalRequest, InteractionRequested, SourceRef
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.interaction.providers import default_interaction_providers
from ksadk.kernel.approval_presentation import approval_presentation
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.state import RunState
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime import RunHandle
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_worker_persists_reviewable_tool_arguments_without_private_fields(child):
    sessions = InMemorySessionService()
    await sessions.create_session("agent", "owner", "session")
    store = InMemoryAgentKernelStore(SessionServiceEventStore(sessions))
    lease = await store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="agent",
            session_id="session",
            activation_id="activation",
            lease_ttl_seconds=60,
        )
    )
    worker = AgentKernelWorker(store, adapter_factory=lambda: None)
    detail = {
        "name": "write_workspace_file",
        "risk": "high",
        "args": {
            "path": "review/proof.txt",
            "content": "Human-reviewed file contents",
            "apiKey": "fixture-api-secret",
            "headers": {"Authorization": "fixture-auth-secret"},
            "nested": [{"password": "fixture-password-secret", "description": "visible"}],
        },
        "execution_policy_ref": "must-remain-private",
    }
    original = json.loads(json.dumps(detail))
    if child:
        detail = {
            "name": "child",
            "kind": "child_tool",
            "child_handle": {"token": "private-handle"},
            "child_approval": {"child_approval": detail},
        }
    event = InteractionRequested(
        schema_version=2,
        event_id="requested",
        seq=1,
        timestamp=1.0,
        run_id="run",
        scope_id="scope",
        source=SourceRef(framework="ksadk"),
        interaction_id="approval",
        interaction_kind="approval",
        request=ApprovalRequest(call_id="parent-call", kind="tool", detail=detail),
    )
    execution = SimpleNamespace(
        handle=RunHandle(
            run_id="run",
            session_id="session",
            runtime_type="harness",
            native_ref={"thread_id": "original-thread"},
        ),
        runtime_run_id="run",
        interaction_provider=default_interaction_providers()["harness"],
    )
    run = RunRecord(
        run_id="run",
        agent_instance_id="agent",
        session_id="session",
        state=RunState.WAITING,
        metadata={"tenant_id": "tenant"},
    )
    await worker._record_interaction_request(
        execution,
        run,
        event,
        ActivationWriteGuard(
            activation_id=lease.activation_id,
            fencing_token=lease.fencing_token,
        ),
    )
    record = await store.get(
        "approval",
        tenant_id="tenant",
        agent_instance_id="agent",
        session_id="session",
        run_id="run",
    )
    assert record.presentation.title == "write_workspace_file"
    presentation = json.loads(record.presentation.description)
    assert presentation == {
        "risk": "high",
        "arguments": {
            "path": "review/proof.txt",
            "content": "Human-reviewed file contents",
            "apiKey": "[REDACTED]",
            "headers": {"Authorization": "[REDACTED]"},
            "nested": [{"password": "[REDACTED]", "description": "visible"}],
        },
    }
    public = json.dumps(record.public_request())
    assert all(
        value not in public
        for value in (
            "fixture-api-secret",
            "fixture-auth-secret",
            "fixture-password-secret",
            "must-remain-private",
            "private-handle",
        )
    )
    assert record.native_target == {"call_id": "parent-call", "thread_id": "original-thread"}
    actual = detail["child_approval"]["child_approval"] if child else detail
    assert actual == original  # Only the display copy is redacted.


def test_approval_preview_marks_truncation_and_preserves_codex_shape():
    value = approval_presentation(
        ApprovalRequest(
            kind="tool",
            detail={
                "name": "write_workspace_file",
                "args": {"path": "large.txt", "content": "x" * 50_000},
            },
        )
    )
    preview = json.loads(value.description)
    assert preview["previewTruncated"] is True and preview["arguments"]["path"] == "large.txt"
    assert len(value.description) < 13_000
    codex = approval_presentation(
        ApprovalRequest(
            kind="command_execution",
            detail={"command": "pwd", "cwd": "/workspace", "token": "private"},
        )
    )
    assert codex.title == "run_command"
    assert json.loads(codex.description) == {"arguments": {"command": "pwd", "cwd": "/workspace"}}
