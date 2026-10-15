"""Durable run-state helpers shared by Studio's web protocol bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ksadk.studio.contracts import OperationStatus, RunStatus
from ksadk.studio.errors import StudioError


def resolve_cancel_run_id(
    studio: Any,
    invocation_id: str,
    run_ids: Mapping[str, str],
    response_runs: Mapping[str, str],
) -> str | None:
    run_id = run_ids.get(invocation_id) or response_runs.get(invocation_id)
    if run_id:
        return run_id
    try:
        studio.event_store.get(invocation_id)
    except Exception:  # noqa: BLE001 - absence is the expected branch
        return None
    return invocation_id


async def wait_for_durable_run(
    task: asyncio.Task[Any],
    run_ids: Mapping[str, str],
    invocation_id: str,
    operations: Mapping[str, str] | None = None,
) -> None:
    # A next-turn prompt may remain queued behind the active Run. Its durable
    # Operation is enough to acknowledge the request before a Run ID exists.
    while (
        invocation_id not in run_ids
        and (operations is None or invocation_id not in operations)
        and not task.done()
    ):
        await asyncio.sleep(0.01)
    if invocation_id not in run_ids and (operations is None or invocation_id not in operations):
        await task


def release_operation(
    task: asyncio.Task[Any], operations: dict[str, str], invocation_id: str
) -> None:
    operations.pop(invocation_id, None)
    if not task.cancelled():
        task.exception()


def attach_release_callback(
    task: asyncio.Task[Any], operations: dict[str, str], invocation_id: str
) -> None:
    task.add_done_callback(lambda done: release_operation(done, operations, invocation_id))


def created_response_sse(
    invocation_id: str, *, model: str, encode: Any, response_shell: Any
) -> str:
    return encode(
        "response.created",
        {
            "type": "response.created",
            "response": response_shell(invocation_id, model=model),
        },
    )


def raise_for_unsuccessful_operation(studio: Any, operation: Any) -> None:
    if operation.status == OperationStatus.CANCELLED:
        raise StudioError("RUN_CANCELLED", "运行已取消", status_code=409)
    try:
        run = studio.event_store.get(operation.resource_id)
    except Exception:  # noqa: BLE001 - preserve the operation error below
        run = None
    if run is not None and run.status == RunStatus.CANCELLED:
        raise StudioError("RUN_CANCELLED", "运行已取消", status_code=409)
    message = str((operation.error or {}).get("message") or "Agent 运行失败")
    raise StudioError("RUN_FAILED", message, status_code=500)


def cancelled_response_sse(invocation_id: str, *, encode: Any) -> str:
    return encode(
        "response.cancelled",
        {
            "type": "response.cancelled",
            "response": {"id": invocation_id, "status": "cancelled"},
        },
    )


def control_ready_event(studio, run_id: str, invocation_id: str):
    """Publish control admission only after the Runtime owner is attached."""
    handle = studio.run_service._active_handles.get(run_id)
    if handle is None or not studio.run_service.executor.is_attached(handle):
        return None
    record = studio.event_store.get(run_id)
    if record.status != RunStatus.RUNNING:
        return None
    payload: dict[str, Any] = {
        "InvocationId": invocation_id,
        "RunId": record.id,
        "AgentId": record.agent_id,
        "SessionId": record.session_id,
    }
    matrix = studio.run_service.executor.attached_capabilities(handle)
    if any(value is not None for value in (matrix.child_input, matrix.child_promotion,
                                           matrix.child_cancellation)):
        payload["ChildControls"] = {
            "input": bool(matrix.child_input and matrix.child_input.supported),
            "promotion": bool(matrix.child_promotion and matrix.child_promotion.supported),
        }
        if matrix.child_cancellation is not None:
            payload["ChildControls"]["cancellation"] = matrix.child_cancellation.supported
    return "studio.run.control_ready", payload


def response_shell(response_id: str, *, model: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "status": "in_progress",
        "model": model,
        "output": [],
    }


def resolve_run_reference(run_ids, response_runs, reference: str) -> str:
    return run_ids.get(reference) or response_runs.get(reference) or reference
