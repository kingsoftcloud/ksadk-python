"""Reject mismatched durable bindings before protocol terminal repair."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from a2a.types import Task, TaskState, TaskStatus

from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
from ksadk.kernel.contracts import AgentControlReceipt
from ksadk.runtime.adapter import RunHandle
from tests.a2a.test_inbound_kernel import identity


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [
    None, "platform_task_id", "target_version_id", "remote_task_id", "remote_context_id",
    "kernel_session", "kernel_agent", "run", "runtime", "missing", "nonterminal",
])
async def test_terminal_repair_requires_matching_durable_binding(monkeypatch, mismatch):
    verified = replace(identity({"message": {}}), caller_principal_type="user",
                       caller_runtime_id=None, authn_mode="api_key")
    context = SimpleNamespace(state={"a2a_identity": verified})
    task = Task(id="remote", context_id="ctx",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    receipt = AgentControlReceipt(command_id=uuid4(), message_id=uuid4(),
                                  status="accepted", accepted_seq=1)
    binding = {"platform_task_id": "a2a-task-test", "target_version_id": "version",
               "remote_task_id": task.id, "remote_context_id": task.context_id}
    submission = {"SessionId": "a2a-kernel-session", "AgentId": "ar-target",
                  "CommandId": str(receipt.command_id), "Receipt": receipt.model_dump(mode="json")}
    if mismatch in binding:
        binding[mismatch] = "other"
    if mismatch == "kernel_session":
        submission["SessionId"] = "a2a-kernel-other"
    if mismatch == "kernel_agent":
        submission["AgentId"] = "ar-other"
    handle = RunHandle(run_id="run", session_id="a2a-kernel-session",
                       runtime_type="other" if mismatch == "runtime" else "langgraph",
                       native_ref={"a2a_context_session": "ctx", "kernel_submission": submission,
                                   "product_kernel_binding": binding})
    fact = SimpleNamespace(run_id="other" if mismatch == "run" else "run", seq=12,
                           family="control", event_type="control.run_transition",
                           payload={"state": "running" if mismatch == "nonterminal"
                                    else "interrupted"})
    events = SimpleNamespace(read=AsyncMock(side_effect=[[fact], []]))
    bridge = ManagedKernelA2ATaskAdapter(object(), control_plane=object(),
                                        session_events=events, runtime_type="langgraph")
    monkeypatch.setattr(bridge, "_restore_handle_from_store",
                        AsyncMock(return_value=None if mismatch == "missing" else handle))
    store = SimpleNamespace(save_confirmed_terminal=AsyncMock(return_value=task))
    rejected = mismatch in binding or mismatch in {"kernel_session", "kernel_agent"}
    if rejected:
        with pytest.raises(PermissionError):
            await bridge.reconcile_terminal_task(task, context, store)
        events.read.assert_not_awaited()
    else:
        assert await bridge.reconcile_terminal_task(task, context, store) is task
    if mismatch is None:
        store.save_confirmed_terminal.assert_awaited_once()
        assert store.save_confirmed_terminal.call_args.args[1].state == TaskState.TASK_STATE_FAILED
        events.read.assert_awaited_once_with("a2a-kernel-session", 0, 100)
    else:
        store.save_confirmed_terminal.assert_not_awaited()
