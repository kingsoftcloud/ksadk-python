import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.interaction.contracts import InteractionSubmission
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.store import ActivationLeaseRequest
from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.kernel_approval import KernelResourceWriteAuthorizer
from ksadk.sessions.local_service import LocalSessionService
from tests.resource_runtime.test_broker import scope as resource_scope


@pytest.mark.parametrize("action,outcome", [("approve", "approved"), ("reject", "rejected")])
@pytest.mark.parametrize(
    "operation", ["save_memory", "execute_skills", "update_memory", "delete_memory"]
)
async def test_durable_scoped_receipt_preserves_actual_decision(
    tmp_path, action, outcome, operation
):
    path = tmp_path / "kernel.sqlite"
    sessions = LocalSessionService(path)
    await sessions.create_session("agent", "user", "session")
    events = SessionServiceEventStore(sessions)
    store = SQLiteAgentKernelStore(path, events)
    await store.ensure_schema()
    scope = dict(tenant_id="tenant", agent_instance_id="agent", session_id="session", run_id="run")
    bound = resource_scope()
    bound = bound.model_copy(
        update={
            "allowed_operations": (operation,),
            "identity": bound.identity.model_copy(
                update={
                    "tenant_ref": "tenant",
                    "agent_id": "agent",
                    "session_ref": "session",
                }
            ),
        }
    )
    request = ResourceRequest.model_validate(
        {
            "v": 1,
            "requestId": "call",
            "handle": "x" * 32,
            "deadline": 1,
            "operation": operation,
            "arguments": {"content": "approved text"}
            if operation == "save_memory"
            else {"memoryId": "record-id", "content": "approved text"}
            if operation == "update_memory"
            else {"memoryId": "record-id"}
            if operation == "delete_memory"
            else {
                "workflowPrompt": "approved text",
                "skillIds": ["skill-a"],
            },
        }
    )
    try:
        lease = await store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id="agent", session_id="session", activation_id="activation"
            )
        )
        guard = ActivationWriteGuard(
            activation_id=lease.activation_id, fencing_token=lease.fencing_token
        )
        authorizer = KernelResourceWriteAuthorizer(
            store, run_id="run", resolve_interaction=lambda request, scope: "approval"
        )
        record = await authorizer.request_write_approval(
            request,
            bound,
            interaction_id="approval",
            event_id="stable-event",
            guard=guard,
        )
        assert (
            "record-id" if operation == "delete_memory" else "approved text"
        ) in record.presentation.description
        assert "approved text" not in str(record.continuation_metadata)
        assert await authorizer.authorize(request, bound) is None
        assert (
            await authorizer.request_write_approval(
                request,
                bound,
                interaction_id="approval",
                event_id="stable-event",
                guard=guard,
            )
            == record
        )
        assert await store.get_terminal_receipt("approval", **scope) is None
        expected = await store.resolve(
            InteractionSubmission(
                interaction_id="approval",
                expected_revision=1,
                action=action,
                idempotency_key="decision",
            ),
            guard=guard,
        )
        await store.close()
        store = SQLiteAgentKernelStore(path, events)
        receipt = await store.get_terminal_receipt("approval", **scope)
        assert receipt == expected
        assert receipt.status == "resolved"
        assert receipt.outcome == outcome
        authorizer = KernelResourceWriteAuthorizer(
            store, run_id="run", resolve_interaction=lambda request, scope: "approval"
        )
        assert await authorizer.authorize(request, bound) == (
            "stable-event" if action == "approve" else None
        )
        changed = request.model_copy(update={"arguments": {"content": "different text"}})
        assert await authorizer.authorize(changed, bound) is None
        changed_scope = bound.model_copy(update={"binding_snapshot_digest": "sha256:" + "9" * 64})
        assert await authorizer.authorize(request, changed_scope) is None
        for key in scope:
            assert await store.get_terminal_receipt("approval", **{**scope, key: "other"}) is None
    finally:
        await store.close()
