"""Cancel, resume, preview, and run-event subscription routes."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, Optional

from fastapi import HTTPException, Query
from fastapi.responses import StreamingResponse

from ksadk.conversations.run_kinds import (
    RUN_MODE_BACKGROUND,
    RUN_MODE_FOREGROUND,
    RUN_TRIGGER_CHECKPOINT_RESUME,
)
from ksadk.conversations.runtime_payloads import build_responses_payload
from ksadk.conversations.runtime_streaming import stream_runtime_responses_conversation_turn
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.server.factory import get_runtime_execution

from . import dependencies as deps
from .checkpoint_resolution import _find_session_checkpoint
from .common import _action_response
from .models import (
    _MAX_PREVIEW_TOOL_RECEIPTS,
    _RUN_TERMINAL_STATUSES,
    GetCheckpointResumePreviewActionRequest,
    ResumeRunActionRequest,
    _run_agent_response_metadata,
    _split_custom_metadata,
)
from .projection import (
    _SIDE_EFFECT_TOOL_NAMES,
    _build_checkpoint_resume_preview,
    _checkpoint_resume_disabled_detail,
    _event_to_action_payload,
    _iter_session_event_pages,
    _latest_invocation_status,
    _oldest_unconsumed_session_events,
    _require_action_session,
    _session_contains_invocation,
    _tool_receipt_event_to_action_payload,
)
from .routers import control_router, run_router
from .streaming import (
    _detached_resume_key_from_input,
    _reject_if_detached_resume_active,
)


@control_router.post("/agentengine/api/v1/GetCheckpointResumePreview")
async def get_checkpoint_resume_preview_action(request: GetCheckpointResumePreviewActionRequest):
    service = deps.resolve_session_service()
    await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )

    checkpoint = await _find_session_checkpoint(
        service=service,
        session_id=request.SessionId,
        run_id=str(request.RunId),
        checkpoint_id=str(request.CheckpointId),
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    checkpoint_seq_id = int(checkpoint.get("SeqId") or 0)
    checkpoint_run_id = str(checkpoint.get("RunId") or "")
    receipts: list[dict[str, Any]] = []
    receipt_total = 0
    side_effect_receipt_count = 0
    failed_receipt_count = 0
    async for page in _iter_session_event_pages(
        service,
        request.SessionId,
        before_seq_id=checkpoint_seq_id + 1 if checkpoint_seq_id else None,
    ):
        for event in page:
            receipt = _tool_receipt_event_to_action_payload(event)
            if receipt is None:
                continue
            if checkpoint_run_id and receipt["RunId"] and receipt["RunId"] != checkpoint_run_id:
                continue
            receipt_total += 1
            if receipt["ToolName"] in _SIDE_EFFECT_TOOL_NAMES:
                side_effect_receipt_count += 1
            if receipt["Status"] == "failed":
                failed_receipt_count += 1
            if len(receipts) < _MAX_PREVIEW_TOOL_RECEIPTS:
                receipts.append(receipt)

    return _action_response(
        "GetCheckpointResumePreview",
        {
            "Preview": _build_checkpoint_resume_preview(
                checkpoint=checkpoint,
                receipts=receipts,
                receipt_total=receipt_total,
                side_effect_receipt_count=side_effect_receipt_count,
                failed_receipt_count=failed_receipt_count,
            )
        },
    )


@control_router.post("/agentengine/api/v1/ResumeRun")
async def resume_run_action(request: ResumeRunActionRequest):
    executor, launch_context = get_runtime_execution()
    service = deps.resolve_session_service()
    session = await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )

    checkpoint = await _find_session_checkpoint(
        service=service,
        session_id=request.SessionId,
        run_id=str(request.RunId),
        checkpoint_id=str(request.CheckpointId),
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    disabled_detail = _checkpoint_resume_disabled_detail(checkpoint)
    if disabled_detail is not None:
        if disabled_detail.get("is_terminal"):
            resume_attempt_id = str(request.ResumeAttemptId or f"resume_{uuid.uuid4().hex}")
            invocation_id = str(request.InvocationId or resume_attempt_id)
            await deps.conversation().append_run_resume_event(
                session_id=request.SessionId,
                author=request.AgentId,
                run_id=str(request.RunId),
                checkpoint_id=str(request.CheckpointId),
                resume_attempt_id=resume_attempt_id,
                framework=checkpoint["Framework"],
                framework_ref=checkpoint["FrameworkRef"],
                invocation_id=invocation_id,
                session_service_provider=deps.resolve_session_service,
            )
            await deps.conversation().append_run_status_event(
                session_id=request.SessionId,
                author=request.AgentId,
                status="completed",
                invocation_id=invocation_id,
                detail="resume_noop_terminal_checkpoint",
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_BACKGROUND,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )
            return _action_response(
                "ResumeRun",
                {
                    "status": "noop",
                    "Reason": disabled_detail["reason"],
                    "CheckpointId": disabled_detail["checkpoint_id"],
                    "RunId": disabled_detail["run_id"],
                    "ResumeAttemptId": resume_attempt_id,
                },
            )
        raise HTTPException(status_code=409, detail=disabled_detail)

    resume_input = {
        "type": "agentengine.resume_checkpoint",
        "run_id": str(request.RunId),
        "checkpoint_id": str(request.CheckpointId),
        "resume_attempt_id": str(request.ResumeAttemptId or f"resume_{uuid.uuid4().hex}"),
        "framework": checkpoint["Framework"],
        "framework_ref": checkpoint["FrameworkRef"],
        "metadata": dict(checkpoint.get("Metadata") or {}),
        "checkpoint_metadata": dict(checkpoint.get("Metadata") or {}),
        "resume_instruction_enabled": bool(getattr(request, "ResumeInstructionEnabled", False)),
        "resume_instruction": str(getattr(request, "ResumeInstruction", "") or "").strip(),
    }
    user_id = session.user_id or "user"
    custom_metadata, _metadata_runtime_controls = _split_custom_metadata(request.Metadata)
    resume_request_metadata = {
        "responses_conversation": True,
    }

    if request.Stream:
        resume_invocation_id = str(request.InvocationId or resume_input["resume_attempt_id"])
        resume_key = _detached_resume_key_from_input(request.SessionId, resume_input)
        _reject_if_detached_resume_active(resume_key)
        # 与 RunAgent Background 同款：返回 SSE 前同步落 resuming 起始事件。
        # detached turn 的首个事件要等流被消费才写；UI 拿到响应头会立刻调
        # SubscribeRunEvents，其 _session_contains_invocation 校验若抢在首次写入前
        # 会误判 409 "InvocationId does not belong to SessionId"。
        # append_run_status_event 按 (invocation_id, status) 幂等，turn 内的补写会去重。
        await deps.conversation().append_run_status_event(
            session_id=request.SessionId,
            author=request.AgentId,
            status="resuming",
            invocation_id=resume_invocation_id,
            detail="checkpoint_resume",
            session_service_provider=deps.resolve_session_service,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
        )
        return deps.detached_streaming_response(
            stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id=request.AgentId,
                user_id=user_id,
                messages=[],
                session_id=request.SessionId,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                request_metadata=resume_request_metadata,
                custom_metadata=custom_metadata,
                include_agentengine_metadata=True,
                resume_input=resume_input,
                invocation_id=resume_invocation_id,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_BACKGROUND,
            ),
            invocation_id=resume_invocation_id,
            resume_key=resume_key,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
        )

    response_id = f"resp_{uuid.uuid4().hex}"
    resolved_session_id, result = await invoke_runtime_conversation_once(
        executor=executor,
        launch_context=launch_context,
        agent_id=request.AgentId,
        user_id=user_id,
        messages=[],
        session_id=request.SessionId,
        model=request.Model,
        model_metadata=request.ModelMetadata,
        model_options=request.ModelOptions,
        request_metadata=resume_request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        response_id=response_id,
        invocation_id=str(resume_input["resume_attempt_id"]),
        session_service_provider=deps.resolve_session_service,
        run_mode=RUN_MODE_FOREGROUND,
    )
    payload = build_responses_payload(
        output_text=result["output_text"],
        model=request.Model,
        session_id=resolved_session_id,
        response_id=response_id,
        metadata=_run_agent_response_metadata(custom_metadata, result),
        usage=result.get("usage") if isinstance(result.get("usage"), Mapping) else None,
    )
    return _action_response("ResumeRun", payload)


@run_router.get("/agentengine/api/v1/SubscribeRunEvents", include_in_schema=False)
async def subscribe_run_events_action(
    SessionId: str = Query(...),
    InvocationId: str = Query(...),
    AfterSeqId: int = Query(0),
    AgentId: Optional[str] = Query(None),
    UserId: Optional[str] = Query(None),
):
    session_id = str(SessionId or "").strip()
    invocation_id = str(InvocationId or "").strip()
    if not session_id or not invocation_id:
        raise HTTPException(status_code=400, detail="SessionId and InvocationId are required")
    service = deps.resolve_session_service()
    await _require_action_session(
        service,
        session_id=session_id,
        agent_id=AgentId,
        user_id=UserId,
    )
    if not await _session_contains_invocation(service, session_id, invocation_id):
        raise HTTPException(
            status_code=409,
            detail="InvocationId does not belong to SessionId",
        )

    async def event_generator() -> AsyncIterator[str]:
        last_seq_id = int(AfterSeqId or 0)
        deadline = time.monotonic() + 5 * 60
        last_heartbeat_at = time.monotonic()
        last_terminal_check_at = 0.0
        while True:
            events = await _oldest_unconsumed_session_events(
                service,
                session_id,
                after_seq_id=last_seq_id,
            )
            for event in events:
                last_seq_id = max(last_seq_id, event.seq_id)
                if event.invocation_id != invocation_id:
                    continue
                payload = _event_to_action_payload(event)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_heartbeat_at = time.monotonic()
                if (
                    event.event_type == "run_status"
                    and str((event.content or {}).get("status") or "").strip().lower()
                    in _RUN_TERMINAL_STATUSES
                ):
                    yield "data: [DONE]\n\n"
                    return

            now = time.monotonic()
            if not events and now - last_terminal_check_at >= deps.heartbeat_interval():
                latest_status = await _latest_invocation_status(
                    service,
                    session_id,
                    invocation_id,
                )
                last_terminal_check_at = now
                if latest_status in _RUN_TERMINAL_STATUSES:
                    yield "data: [DONE]\n\n"
                    return
            if now - last_heartbeat_at >= deps.heartbeat_interval():
                yield ": heartbeat\n\n"
                last_heartbeat_at = now
            if now > deadline:
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
