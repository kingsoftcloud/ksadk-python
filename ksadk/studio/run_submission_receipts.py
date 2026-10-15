"""Read original submission facts; never infer delivery from the latest turn."""

from __future__ import annotations

from typing import Any

from ksadk.studio.contracts import OperationKind, OperationStatus, RunStatus, RunSubmissionRef
from ksadk.studio.errors import StudioError


def submission_receipt(studio: Any, ref: RunSubmissionRef) -> dict[str, Any]:
    studio._require_direct_session(ref.session_id)
    identity = {
        "SchemaVersion": 1,
        "AgentId": ref.agent_id,
        "SessionId": ref.session_id,
        "InvocationId": ref.client_invocation_id,
    }
    records = studio.event_store.list_runs(session_id=ref.session_id, agent_id=ref.agent_id)
    matched = [
        record
        for record in records
        if record.submission_ref == ref or record.id == ref.client_invocation_id
    ]
    if not matched:
        # Legacy completed submissions already have a durable correlation key.
        # Looking it up is not a RunAgent retry and cannot start an operation.
        operation = studio.operations.find_by_idempotency_key(
            f"responses:{ref.client_invocation_id}"
        )
        if operation is not None and operation.kind == OperationKind.RUN:
            matched = [record for record in records if record.id == operation.resource_id]
    if len(matched) > 1:
        raise StudioError(
            "RUN_SUBMISSION_AMBIGUOUS", "原请求对应多个运行，不能确定投递结果", status_code=409
        )
    if matched:
        record = matched[0]
        studio._require_direct_run(record.id)
        return {
            **identity,
            "RunId": record.id,
            "Status": RunStatus(record.status).value.lower(),
            "Source": "run",
        }
    # A queued request has no RunRecord yet. The original operation stores the
    # correlation at submission, before starting or waiting for admission.
    operations = [
        operation
        for operation in studio.operations.list(kind=OperationKind.RUN)
        if operation.metadata.get("submission_ref") == ref.model_dump()
    ]
    if len(operations) > 1:
        raise StudioError(
            "RUN_SUBMISSION_AMBIGUOUS", "原请求对应多个操作，不能确定投递结果", status_code=409
        )
    if operations:
        operation = operations[0]
        state = OperationStatus(operation.status).value.lower()
        if state in {"queued", "running", "failed", "cancelled", "interrupted"}:
            return {
                **identity,
                "RunId": "",
                "Status": state,
                "Source": "operation",
            }
    # Missing evidence never proves non-delivery, success or permission to retry.
    return {**identity, "RunId": "", "Status": "unknown", "Source": "unavailable"}
