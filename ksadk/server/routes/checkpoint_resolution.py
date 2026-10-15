"""Checkpoint lookup and resume-input resolution."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from typing import Any

from fastapi import HTTPException

from ksadk.sessions import Session, SessionEvent
from ksadk.sessions.errors import (
    CheckpointLookupTimeout,
    HistoryProjectionBudgetExceeded,
    HistoryReloadRequired,
)
from ksadk.sessions.db_budget import session_db_budget

from .projection import (
    _apply_checkpoint_resume_audit,
    _apply_latest_checkpoint_policy,
    _checkpoint_event_to_action_payload,
    _record_resume_audit,
)


async def _find_session_checkpoint(
    *,
    service: Any,
    session_id: str,
    run_id: str,
    checkpoint_id: str,
    expected_session: Session | None = None,
) -> dict[str, Any] | None:
    audit_by_session: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    latest_by_session_run: dict[tuple[str, str], int] = {}
    candidate_event: SessionEvent | None = None
    scanned = 0
    size = 0

    async def custom_backend_pages() -> AsyncIterator[list[SessionEvent]]:
        # Legacy duck-typed services may not inherit BaseSessionService.
        if expected_session is not None:
            candidate = await service.get_session_metadata(session_id)
            if (candidate is None or candidate.agent_id != expected_session.agent_id
                    or candidate.user_id != expected_session.user_id
                    or candidate.created_at != expected_session.created_at):
                raise HistoryReloadRequired("Checkpoint session changed during lookup")
        events = await service.get_events(session_id)
        related = sorted(
            (event for event in events if event.event_type in
             {"run_checkpoint", "run_resume", "runtime_resume", "continuation.created", "continuation.resumed"}),
            key=lambda event: (event.seq_id, event.id),
        )
        for index in range(0, len(related), 256):
            yield related[index : index + 256]

    iterator = getattr(service, "iter_resume_related_event_pages", None)
    pages = (
        iterator(session_id, page_size=256, expected_session=expected_session)
        if callable(iterator) else custom_backend_pages()
    )
    try:
        async with asyncio.timeout(session_db_budget().write_operation_seconds):
            async with aclosing(pages) as fixed_pages:
                async for page in fixed_pages:
                    scanned += len(page)
                    size += sum(
                        len(json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8"))
                        for event in page
                    )
                    if scanned > 8192 or size > 16 * 1024 * 1024:
                        raise HistoryProjectionBudgetExceeded(
                            "Checkpoint lookup exceeds 8192 related events or 16 MiB"
                        )
                    for event in page:
                        _record_resume_audit(audit_by_session, event)
                        checkpoint = _checkpoint_event_to_action_payload(event)
                        if checkpoint is None:
                            continue
                        metadata = checkpoint.get("Metadata") or {}
                        checkpoint_run_id = str(checkpoint.get("RunId") or "")
                        if metadata.get("only_latest_resumable"):
                            key = (session_id, checkpoint_run_id)
                            latest_by_session_run[key] = max(
                                latest_by_session_run.get(key, 0),
                                int(checkpoint.get("SeqId") or 0),
                            )
                        if (checkpoint_run_id == run_id
                                and checkpoint.get("CheckpointId") == checkpoint_id):
                            candidate_event = event
    except TimeoutError as exc:
        raise CheckpointLookupTimeout("Checkpoint lookup deadline exceeded") from exc
    if candidate_event is None:
        return None
    checkpoint = _checkpoint_event_to_action_payload(candidate_event)
    if checkpoint is None:
        return None
    checkpoint = _apply_checkpoint_resume_audit(
        checkpoint,
        audit_by_session.get(session_id, {}),
    )
    return _apply_latest_checkpoint_policy(
        checkpoint,
        latest_by_session_run,
        session_id=session_id,
    )


async def _resolve_checkpoint_resume_input_from_session(
    *,
    service: Any,
    agent_id: str,
    session_id: str | None,
    resume_input: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(resume_input, Mapping):
        return None
    if str(resume_input.get("type") or "").strip() != "agentengine.resume_checkpoint":
        return dict(resume_input)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="Checkpoint resume requires session_id")

    header = getattr(service, "get_session_header", None)
    if not callable(header):
        header = service.get_session_metadata
    session = await header(normalized_session_id)
    if not session or session.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Session not found")

    run_id = str(resume_input.get("run_id") or "").strip()
    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if not run_id or not checkpoint_id:
        raise HTTPException(
            status_code=400, detail="Checkpoint resume requires run_id and checkpoint_id"
        )

    checkpoint = await _find_session_checkpoint(
        service=service,
        session_id=normalized_session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        expected_session=session,
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    resume_attempt_id = str(resume_input.get("resume_attempt_id") or "").strip()
    return {
        "type": "agentengine.resume_checkpoint",
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "resume_attempt_id": resume_attempt_id or f"resume_{uuid.uuid4().hex}",
        "framework": checkpoint["Framework"],
        "framework_ref": checkpoint["FrameworkRef"],
        "metadata": dict(checkpoint.get("Metadata") or {}),
        "checkpoint_metadata": dict(checkpoint.get("Metadata") or {}),
        "resume_instruction_enabled": bool(
            resume_input.get("resume_instruction_enabled")
            or resume_input.get("ResumeInstructionEnabled")
        ),
        "resume_instruction": str(
            resume_input.get("resume_instruction") or resume_input.get("ResumeInstruction") or ""
        ).strip(),
    }
