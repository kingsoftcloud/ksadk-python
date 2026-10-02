"""Durable receipt helpers used by approval resume orchestration."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ksadk.conversations.runtime_persistence import append_conversation_event
from ksadk.sessions import SessionEvent, ToolReceiptClaim


class ToolReceiptUncertainError(ValueError):
    """A durable receipt exists without a safely replayable terminal result."""

    def __init__(self, idempotency_key: str | None = None) -> None:
        self.idempotency_key = str(idempotency_key or "")
        detail = "tool receipt execution is uncertain; reconciliation required"
        if self.idempotency_key:
            detail += f"; idempotency_key={self.idempotency_key}"
        super().__init__(detail)


async def claim_receipt_for_execution(
    *,
    service: Any,
    session_id: str,
    receipt: Mapping[str, Any],
    tool_name: str,
    call_args: Mapping[str, Any],
    invocation_id: str,
    preclaimed_receipt: tuple[dict[str, Any], ToolReceiptClaim] | None,
) -> ToolReceiptClaim:
    claim = (
        preclaimed_receipt[1]
        if preclaimed_receipt is not None
        else await service.claim_tool_receipt(
            session_id,
            idempotency_key=str(receipt["idempotency_key"]),
            tool_name=tool_name,
            arguments_digest=_tool_receipt_arguments_digest(call_args),
            claim_id=f"{invocation_id}:{uuid.uuid4().hex}",
        )
    )
    return _validate_tool_receipt_claim(claim)


async def replay_existing_tool_receipt_event(
    *,
    existing_event: SessionEvent,
    receipt: Mapping[str, Any],
    session_id: str,
    invocation_id: str,
    run_id: str,
    tool_name: str,
    call_args: Mapping[str, Any],
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
) -> dict[str, Any]:
    from ksadk.conversations.runtime_resume import _validate_tool_receipt_event

    existing_metadata = existing_event.metadata or {}
    _validate_tool_receipt_event(existing_event)
    output = existing_metadata["tool_output"]
    if isinstance(output, Mapping):
        output = {**dict(output), "replayed": True}
    replayed_receipt = {
        **dict((existing_metadata.get("tool_receipt") or receipt)),
        "replayed": True,
        "replayed_from_event_id": existing_event.id,
    }
    await append_conversation_event(
        session_id=session_id,
        author="tool",
        role="user",
        text=str(output),
        invocation_id=invocation_id,
        event_type="tool_result",
        session_service_provider=session_service_provider,
        metadata={
            "tool_name": tool_name,
            "tool_args": dict(call_args),
            "tool_output": output,
            "run_id": run_id,
            "approval_request_id": resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id"),
            "tool_receipt": replayed_receipt,
            "replayed": True,
        },
    )
    return {"type": "function_call_output", "call_id": run_id, "output": output}


def _validate_tool_receipt_claim_state(state: str, idempotency_key: str = "") -> str:
    normalized = str(state or "").strip().lower()
    if normalized not in {"unknown", "completed", "failed"}:
        raise ToolReceiptUncertainError(idempotency_key)
    return normalized


def _validate_tool_receipt_claim(claim: ToolReceiptClaim) -> ToolReceiptClaim:
    state = _validate_tool_receipt_claim_state(claim.state, claim.idempotency_key)
    if isinstance(claim.output, Mapping) and state in {"completed", "failed"}:
        expected = (
            "completed"
            if claim.output.get("status") == "accepted_not_extracted"
            else ("failed" if claim.output.get("ok") is False else "completed")
        )
        if state != expected:
            raise ToolReceiptUncertainError(claim.idempotency_key)
    return claim


def _tool_receipt_arguments_digest(tool_args: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(tool_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        encoded = json.dumps(str(tool_args), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _claim_approved_builtin_tool_resume(
    *,
    session_id: str,
    invocation_id: str,
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
    existing_events: Sequence[SessionEvent],
) -> tuple[dict[str, Any] | None, ToolReceiptClaim | None]:
    """Reserve an approved builtin before consuming its approval response."""

    from ksadk.conversations.runtime_resume import (
        LegacyToolReceiptCheckpointAmbiguityError,
        _builtin_tool_callable,
        _find_legacy_tool_receipt_event_for_resume,
        _find_tool_receipt_event_by_key,
        _latest_checkpoint_metadata_for_run,
        _tool_receipt_metadata,
        _tool_resume_run_id,
        _validate_tool_receipt_event,
    )

    approval = resume_input.get("approval")
    if not isinstance(approval, Mapping) or not bool(approval.get("approved")):
        return None, None
    tool_name = str(resume_input.get("tool_name") or "").strip()
    tool_args = resume_input.get("tool_args")
    if not tool_name or not isinstance(tool_args, Mapping):
        return None, None
    call_args = dict(tool_args)
    run_id = _tool_resume_run_id(resume_input)
    if not run_id:
        return None, None
    checkpoint_metadata = _latest_checkpoint_metadata_for_run(existing_events, run_id)
    requested_checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if requested_checkpoint_id:
        checkpoint_metadata = {**checkpoint_metadata, "checkpoint_id": requested_checkpoint_id}
    receipt = _tool_receipt_metadata(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=run_id,
        tool_name=tool_name,
        tool_args=call_args,
        checkpoint_id=checkpoint_metadata.get("checkpoint_id"),
        framework=checkpoint_metadata.get("framework"),
        framework_ref=checkpoint_metadata.get("framework_ref"),
    )
    existing_event = _find_tool_receipt_event_by_key(
        existing_events,
        receipt["idempotency_key"],
        expected_receipt=receipt,
        expected_tool_args=call_args,
    )
    if existing_event is not None:
        _validate_tool_receipt_event(existing_event)
        return receipt, None
    if requested_checkpoint_id:
        legacy_event = _find_legacy_tool_receipt_event_for_resume(
            session_id=session_id,
            resume_input=resume_input,
            events=existing_events,
        )
        if legacy_event is not None:
            raise LegacyToolReceiptCheckpointAmbiguityError()
    if _builtin_tool_callable(tool_name) is None:
        return None, None
    claim = await session_service_provider().claim_tool_receipt(
        session_id,
        idempotency_key=str(receipt["idempotency_key"]),
        tool_name=tool_name,
        arguments_digest=_tool_receipt_arguments_digest(call_args),
        claim_id=f"{invocation_id}:{uuid.uuid4().hex}",
    )
    _validate_tool_receipt_claim(claim)
    return receipt, claim



async def replay_claimed_tool_receipt(
    *,
    claim: ToolReceiptClaim,
    receipt: Mapping[str, Any],
    session_id: str,
    invocation_id: str,
    run_id: str,
    tool_name: str,
    call_args: Mapping[str, Any],
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
) -> dict[str, Any]:
    _validate_tool_receipt_claim(claim)
    if claim.state == "unknown":
        raise ToolReceiptUncertainError(claim.idempotency_key)
    output = claim.output
    if isinstance(output, Mapping):
        output = {**dict(output), "replayed": True}
    replayed_receipt = {
        **dict(receipt),
        "status": claim.state,
        "replayed": True,
        "replayed_from_claim": True,
    }
    await append_conversation_event(
        session_id=session_id,
        author="tool",
        role="user",
        text=str(output),
        invocation_id=invocation_id,
        event_type="tool_result",
        session_service_provider=session_service_provider,
        metadata={
            "tool_name": tool_name,
            "tool_args": dict(call_args),
            "tool_output": output,
            "run_id": run_id,
            "approval_request_id": resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id"),
            "tool_receipt": replayed_receipt,
            "replayed": True,
        },
    )
    return {"type": "function_call_output", "call_id": run_id, "output": output}


async def claim_or_replay_tool_receipt(
    *,
    service: Any,
    session_id: str,
    receipt: dict[str, Any],
    tool_name: str,
    call_args: Mapping[str, Any],
    invocation_id: str,
    run_id: str,
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
    preclaimed_receipt: tuple[dict[str, Any], ToolReceiptClaim] | None,
) -> tuple[ToolReceiptClaim, dict[str, Any] | None]:
    claim = await claim_receipt_for_execution(
        service=service,
        session_id=session_id,
        receipt=receipt,
        tool_name=tool_name,
        call_args=call_args,
        invocation_id=invocation_id,
        preclaimed_receipt=preclaimed_receipt,
    )
    if claim.acquired:
        return claim, None
    return claim, await replay_claimed_tool_receipt(
        claim=claim,
        receipt=receipt,
        session_id=session_id,
        invocation_id=invocation_id,
        run_id=run_id,
        tool_name=tool_name,
        call_args=call_args,
        resume_input=resume_input,
        session_service_provider=session_service_provider,
    )


async def settle_claimed_tool_receipt(
    *,
    service: Any,
    claim: ToolReceiptClaim,
    receipt: dict[str, Any],
    session_id: str,
    tool_name: str,
    call_args: Mapping[str, Any],
    output: Any,
) -> None:
    from ksadk.conversations.runtime_resume import _tool_receipt_status_from_output

    status = _tool_receipt_status_from_output(output)
    settled = await service.settle_tool_receipt(
        session_id,
        idempotency_key=str(receipt["idempotency_key"]),
        tool_name=tool_name,
        arguments_digest=_tool_receipt_arguments_digest(call_args),
        claim_id=claim.claim_id,
        state="failed" if status == "failed" else "completed",
        output=output,
    )
    if settled.claim_id != claim.claim_id or settled.state == "unknown":
        raise ToolReceiptUncertainError(claim.idempotency_key)
    receipt["status"] = status
