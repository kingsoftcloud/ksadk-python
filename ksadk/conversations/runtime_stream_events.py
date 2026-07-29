from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Sequence

from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    trigger_from_resume_input,
    validate_run_mode,
)
from ksadk.conversations.runtime_compaction import preview_auto_compaction
from ksadk.conversations.runtime_constants import (
    ASSISTANT_STREAM_SNAPSHOT_INTERVAL_SECONDS,
    ASSISTANT_STREAM_SNAPSHOT_MIN_NEW_CHARS,
    PTL_RETRY_KEEP_TAIL_GROUPS,
)
from ksadk.conversations.runtime_governance import (
    RuntimeCircuitOpen,
    _compact_conversation_history_with_governance,
    _governance_record_tool_call,
    _governance_record_tool_result,
    _governance_record_turn_start,
    _runtime_governance_from_env,
    _tool_observability_metadata,
)
from ksadk.conversations.runtime_input import (
    _auto_save_ltm_turn,
    _build_runner_ambient_contexts,
    _build_runner_request_payload,
    _inject_runner_deferred_tools_for_request,
    _is_prompt_too_long_error,
    _runner_name,
    _runner_type_name,
)
from ksadk.conversations.runtime_metadata import _update_session_metadata_after_assistant_turn
from ksadk.conversations.runtime_observability import (
    _extract_deferred_tool_names,
    _get_conversation_tracer,
    _normalize_usage_payload,
    _set_conversation_input_attributes,
    _set_conversation_output_attributes,
    _set_conversation_span_attributes,
    _set_conversation_usage_attributes,
    _set_span_attribute,
    _span_current_context,
    _span_feedback_metadata,
)
from ksadk.conversations.runtime_payloads import CompactionPlan
from ksadk.conversations.runtime_persistence import (
    append_conversation_event,
    append_deferred_tools_event,
    append_reasoning_event,
    append_run_checkpoint_event,
    append_run_status_event,
)
from ksadk.conversations.runtime_preparation import _refresh_history, build_run_input
from ksadk.conversations.runtime_resume import (
    _checkpoint_event_args_from_agentengine_metadata,
    _extract_agentengine_metadata,
    _failed_status_for_resume,
    _filter_responses_reasoning_output,
    _is_checkpoint_resume_input,
    _latest_checkpoint_metadata_for_run,
    _merge_agentengine_metadata,
    _model_options_disable_reasoning,
    _semantic_events_from_responses_output,
    _tool_receipt_metadata,
)
from ksadk.model_policy import fallback_model_for_exception, model_policy_options_for_model
from ksadk.runtime_context import (
    PlatformInvocationContext,
    platform_invocation_scope,
    tool_execution_scope,
)
from ksadk.sessions import resolve_session_service
from ksadk.tools.gateway import (
    approval_interrupt_info_from_result,
)


async def _iter_conversation_turn_events(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    response_id: str | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[dict[str, Any]]:
    """Internal semantic event stream shared by protocol serializers."""
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    governance = _runtime_governance_from_env()
    _governance_record_turn_start(governance)
    entry_run_mode = validate_run_mode(run_mode)
    entry_run_trigger = trigger_from_resume_input(resume_input)
    if resume_input is None:
        compaction_preview = await preview_auto_compaction(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            session_service_provider=provider,
        )
    else:
        compaction_preview = CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=0,
            total_estimated_tokens=0,
            group_count=0,
            tail_groups=0,
        )
    if compaction_preview.should_compact:
        yield {
            "type": "compaction",
            "phase": "start",
            "trigger": "auto",
            "total_chars": compaction_preview.total_chars,
            "total_estimated_tokens": compaction_preview.total_estimated_tokens,
            "group_count": compaction_preview.group_count,
            "threshold_percentage": compaction_preview.auto_compact_threshold_percentage,
        }
    try:
        prepared = await build_run_input(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            model_options=model_options,
            state_delta=state_delta,
            instructions=instructions,
            request_metadata=request_metadata,
            custom_metadata=custom_metadata,
            resume_input=resume_input,
            invocation_id=invocation_id,
            governance_state=governance,
            session_service_provider=provider,
            run_mode=entry_run_mode,
        )
        # prepared 之后的 run_status 写入复用 prepared 的 mode/trigger
        run_mode = prepared.run_mode
        run_trigger = prepared.run_trigger
    except RuntimeCircuitOpen as exc:
        if session_id:
            await append_run_status_event(
                session_id=session_id,
                author=_runner_name(runner),
                status=_failed_status_for_resume(resume_input),
                invocation_id=invocation_id,
                detail=str(exc),
                metadata={"governance": exc.metadata},
                session_service_provider=provider,
                run_mode=entry_run_mode,
                run_trigger=entry_run_trigger,
            )
        yield {"type": "error", "message": str(exc) or "Agent 运行失败"}
        return
    _inject_runner_deferred_tools_for_request(runner, prepared)
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        account_id=str(account_id or ""),
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_content=list(prepared.input_content),
        input_messages=list(prepared.input_messages),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        current_attachments=list(prepared.current_attachments),
        current_attachment_results=list(prepared.current_attachment_results),
        has_current_files=prepared.has_current_files,
        runner_type=_runner_type_name(runner),
        metadata=dict(custom_metadata or {}),
        model=model,
        model_options=prepared.model_options,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
        tool_approval_mode=str(
            prepared.request_metadata.get("tool_approval_mode") or ""
        ),
    )
    if prepared.compaction_triggered:
        yield {
            "type": "compaction",
            "phase": "done",
            "trigger": str(prepared.compaction_trigger or "auto"),
            "compacted_until_seq_id": prepared.compacted_until_seq_id,
            "total_chars": (
                compaction_preview.total_chars if compaction_preview.should_compact else None
            ),
            "total_estimated_tokens": (
                compaction_preview.total_estimated_tokens
                if compaction_preview.should_compact
                else None
            ),
            "group_count": (
                compaction_preview.group_count if compaction_preview.should_compact else None
            ),
            "threshold_percentage": (
                compaction_preview.auto_compact_threshold_percentage
                if compaction_preview.should_compact
                else None
            ),
        }
    runner_name = _runner_name(runner)
    tracer = _get_conversation_tracer()
    span = tracer.start_span(runner_name) if tracer else None
    span_ended = False

    def _finish_span() -> None:
        nonlocal span_ended
        if span is None or span_ended:
            return
        span_ended = True
        try:
            span.end()
        except Exception:
            pass

    try:
        _set_conversation_span_attributes(
            span,
            agent_id=agent_id,
            user_id=user_id,
            session_id=prepared.session_id,
            invocation_id=prepared.invocation_id,
            runner_name=runner_name,
            model=model,
            response_id=response_id,
        )
        _set_conversation_input_attributes(span, prepared.user_input or prepared.user_display_input)
        trace_metadata = _span_feedback_metadata(span)
        yield {
            "type": "started",
            "session_id": prepared.session_id,
            "metadata": {**trace_metadata, **dict(request_metadata or {})},
        }
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="in_progress",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )

        accumulated_text = ""
        last_snapshot_text = ""
        last_snapshot_at = 0.0
        snapshot_index = 0
        accumulated_reasoning_parts: list[str] = []
        emitted_anything = False
        emitted_response_artifacts = False
        saw_final_chunk = False
        responses_output: list[Any] = []
        responses_response_id: str | None = response_id
        runner_agentengine_metadata: dict[str, Any] = {}
        stream_usage: dict[str, Any] = {}
        stream_last_usage: dict[str, Any] = {}
        reasoning_disabled = _model_options_disable_reasoning(prepared.model_options)

        async def _persist_accumulated_reasoning(*, before_text: bool = False) -> None:
            if not accumulated_reasoning_parts:
                return
            reasoning = "".join(accumulated_reasoning_parts)
            accumulated_reasoning_parts.clear()
            await append_reasoning_event(
                session_id=prepared.session_id,
                author=runner_name,
                text=reasoning,
                invocation_id=prepared.invocation_id,
                metadata={"stream_boundary": "before_text"} if before_text else None,
                session_service_provider=provider,
            )

        async def _persist_assistant_snapshot(*, force: bool = False) -> None:
            """Persist a bounded replay point for detached stream recovery."""
            nonlocal last_snapshot_at, last_snapshot_text, snapshot_index
            if not accumulated_text or accumulated_text == last_snapshot_text:
                return
            now = time.monotonic()
            if (
                last_snapshot_text
                and not force
                and len(accumulated_text) - len(last_snapshot_text)
                < ASSISTANT_STREAM_SNAPSHOT_MIN_NEW_CHARS
                and now - last_snapshot_at < ASSISTANT_STREAM_SNAPSHOT_INTERVAL_SECONDS
            ):
                return
            snapshot_index += 1
            await append_conversation_event(
                session_id=prepared.session_id,
                author=runner_name,
                role="model",
                text=accumulated_text,
                invocation_id=prepared.invocation_id,
                event_type="assistant_stream_snapshot",
                metadata={"stream_snapshot": True, "snapshot_index": snapshot_index},
                session_service_provider=provider,
            )
            last_snapshot_text = accumulated_text
            last_snapshot_at = now

        for attempt in range(2):
            try:
                runtime_context.history = list(prepared.history)
                with platform_invocation_scope(runtime_context):
                    with tool_execution_scope(
                        session_id=prepared.session_id,
                        run_id=prepared.invocation_id,
                        invocation_id=prepared.invocation_id,
                    ):
                        stream = runner.stream(
                            _build_runner_request_payload(
                                prepared=prepared,
                                model=model,
                                runtime_context=runtime_context,
                                runner=runner,
                            )
                        )
                        while True:
                            try:
                                with _span_current_context(span):
                                    chunk = await anext(stream)
                            except StopAsyncIteration:
                                break
                            chunk_type = chunk.get("type")
                            if chunk_type == "checkpoint":
                                emitted_anything = True
                                chunk_agentengine_metadata = _extract_agentengine_metadata(chunk)
                                if chunk_agentengine_metadata:
                                    runner_agentengine_metadata.update(chunk_agentengine_metadata)
                                    resume_run_id = ""
                                    if prepared.resume_input and _is_checkpoint_resume_input(
                                        prepared.resume_input
                                    ):
                                        resume_run_id = str(
                                            prepared.resume_input.get("run_id") or ""
                                        ).strip()
                                    checkpoint_args = (
                                        _checkpoint_event_args_from_agentengine_metadata(
                                            runner_agentengine_metadata.get("agentengine"),
                                            fallback_run_id=resume_run_id or prepared.invocation_id,
                                        )
                                    )
                                    if checkpoint_args:
                                        await append_run_checkpoint_event(
                                            session_id=prepared.session_id,
                                            author=runner_name,
                                            run_id=checkpoint_args["run_id"],
                                            checkpoint_id=checkpoint_args["checkpoint_id"],
                                            framework=checkpoint_args["framework"],
                                            framework_ref=checkpoint_args["framework_ref"],
                                            phase=checkpoint_args.get("phase") or "stream",
                                            invocation_id=prepared.invocation_id,
                                            metadata=checkpoint_args.get("metadata"),
                                            session_service_provider=provider,
                                        )
                                continue
                            if chunk_type == "responses_output":
                                if isinstance(chunk.get("usage"), Mapping):
                                    stream_usage = _normalize_usage_payload(chunk.get("usage"))
                                chunk_last = (chunk.get("metadata") or {}).get("last_usage")
                                if isinstance(chunk_last, Mapping):
                                    stream_last_usage = (
                                        _normalize_usage_payload(chunk_last) or stream_usage
                                    )
                                raw_output = chunk.get("output")
                                responses_output = (
                                    raw_output if isinstance(raw_output, list) else []
                                )
                                if reasoning_disabled:
                                    responses_output = _filter_responses_reasoning_output(
                                        responses_output
                                    )
                                raw_response_id = chunk.get("response_id")
                                responses_response_id = (
                                    str(raw_response_id)
                                    if raw_response_id
                                    else responses_response_id
                                )
                                if responses_output and not emitted_response_artifacts:
                                    for semantic_event in _semantic_events_from_responses_output(
                                        responses_output
                                    ):
                                        if semantic_event.get("type") == "thinking":
                                            accumulated_reasoning_parts.append(
                                                str(semantic_event.get("delta") or "")
                                            )
                                        emitted_anything = True
                                        yield semantic_event
                                continue
                            if chunk_type == "thinking":
                                if reasoning_disabled:
                                    continue
                                delta = str(chunk.get("delta", ""))
                                if delta:
                                    # Close the preceding text segment before
                                    # a new thought begins.  Snapshot throttling
                                    # must not erase this timeline boundary.
                                    await _persist_assistant_snapshot(force=True)
                                    accumulated_reasoning_parts.append(delta)
                                    emitted_anything = True
                                    emitted_response_artifacts = True
                                    yield {"type": "thinking", "delta": delta}
                                continue
                            if chunk_type == "text":
                                delta = str(chunk.get("delta", ""))
                                if delta:
                                    # A reasoning event must be written before
                                    # the text snapshot it explains.  Without
                                    # this boundary, historical replay only
                                    # sees one merged reasoning blob after all
                                    # streamed text and cannot rebuild an
                                    # interleaved turn.
                                    await _persist_accumulated_reasoning(before_text=True)
                                    replace = bool(chunk.get("replace"))
                                    accumulated_text = (
                                        delta if replace else accumulated_text + delta
                                    )
                                    emitted_anything = True
                                    await _persist_assistant_snapshot(force=replace)
                                    text_event: dict[str, Any] = {
                                        "type": "text",
                                        "delta": delta,
                                    }
                                    if replace:
                                        text_event["replace"] = True
                                    yield text_event
                                continue
                            if chunk_type == "tool_call":
                                await _persist_assistant_snapshot(force=True)
                                _governance_record_tool_call(governance)
                                emitted_response_artifacts = True
                                tool_args = chunk.get("tool_args", {})
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                tool_call_id = str(
                                    chunk.get("call_id") or chunk.get("run_id") or ""
                                ).strip()
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model",
                                    text=str(chunk.get("tool_name") or "tool"),
                                    invocation_id=prepared.invocation_id,
                                    event_type="tool_call",
                                    metadata={
                                        "tool_name": chunk.get("tool_name"),
                                        "tool_args": dict(tool_args),
                                        "run_id": chunk.get("run_id"),
                                        "tool_call_id": tool_call_id,
                                        "stage": chunk.get("stage") or tool_args.get("stage"),
                                        "event_kind": chunk.get("event_kind"),
                                        "display_title": chunk.get("display_title"),
                                        "display_summary": chunk.get("display_summary"),
                                    },
                                    session_service_provider=provider,
                                )
                                emitted_anything = True
                                await _persist_accumulated_reasoning()
                                yield {
                                    "type": "tool_call",
                                    "name": chunk.get("tool_name"),
                                    "args": dict(tool_args),
                                    "run_id": chunk.get("run_id"),
                                    "stage": chunk.get("stage") or tool_args.get("stage"),
                                    "event_kind": chunk.get("event_kind"),
                                    "display_title": chunk.get("display_title"),
                                    "display_summary": chunk.get("display_summary"),
                                }
                                continue
                            if chunk_type in {"stage_tool_call", "stage_tool_result"}:
                                await _persist_assistant_snapshot(force=True)
                                emitted_response_artifacts = True
                                tool_name = str(
                                    chunk.get("tool_name") or chunk.get("name") or "tool"
                                )
                                tool_args = chunk.get("tool_args", chunk.get("args", {}))
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                tool_output = chunk.get("tool_output", chunk.get("output", ""))
                                event_kind = str(chunk.get("event_kind") or "")
                                display_title = str(chunk.get("display_title") or tool_name)
                                display_summary = str(
                                    chunk.get("display_summary") or chunk.get("text") or ""
                                )
                                tool_call_id = str(
                                    chunk.get("call_id") or chunk.get("run_id") or ""
                                ).strip()
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model" if chunk_type == "stage_tool_call" else "user",
                                    text=display_summary or tool_name,
                                    invocation_id=prepared.invocation_id,
                                    event_type=chunk_type,
                                    metadata={
                                        "tool_name": tool_name,
                                        "tool_args": dict(tool_args),
                                        "tool_output": tool_output,
                                        "run_id": chunk.get("run_id"),
                                        "tool_call_id": tool_call_id,
                                        "stage": chunk.get("stage") or tool_args.get("stage"),
                                        "event_kind": event_kind,
                                        "display_title": display_title,
                                        "display_summary": display_summary,
                                    },
                                    session_service_provider=provider,
                                )
                                emitted_anything = True
                                yield {
                                    "type": chunk_type,
                                    "name": tool_name,
                                    "args": dict(tool_args),
                                    "output": tool_output,
                                    "run_id": chunk.get("run_id"),
                                    "stage": chunk.get("stage") or tool_args.get("stage"),
                                    "event_kind": event_kind,
                                    "display_title": display_title,
                                    "display_summary": display_summary,
                                }
                                continue
                            if chunk_type == "tool_result":
                                await _persist_assistant_snapshot(force=True)
                                emitted_response_artifacts = True
                                tool_name = str(chunk.get("tool_name") or "tool")
                                tool_args = chunk.get("tool_args", {})
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                tool_run_id = str(chunk.get("run_id") or prepared.invocation_id)
                                tool_call_id = str(
                                    chunk.get("call_id") or chunk.get("run_id") or tool_run_id
                                ).strip()
                                checkpoint_metadata = _latest_checkpoint_metadata_for_run(
                                    await provider().get_events(prepared.session_id),
                                    tool_run_id,
                                )
                                approval_interrupt_info = approval_interrupt_info_from_result(
                                    chunk.get("tool_output", ""),
                                    fallback_tool_name=tool_name,
                                    tool_args=tool_args,
                                    run_id=tool_run_id,
                                )
                                if approval_interrupt_info:
                                    await append_conversation_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        role="model",
                                        text="approval requested",
                                        invocation_id=prepared.invocation_id,
                                        event_type="approval_request",
                                        metadata={"interrupt_info": approval_interrupt_info},
                                        session_service_provider=provider,
                                    )
                                    await append_run_status_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        status="interrupted",
                                        invocation_id=prepared.invocation_id,
                                        detail="approval_required",
                                        session_service_provider=provider,
                                        run_mode=run_mode,
                                        run_trigger=run_trigger,
                                    )
                                    emitted_anything = True
                                    await _persist_accumulated_reasoning()
                                    yield {
                                        "type": "interrupt",
                                        "interrupt_info": approval_interrupt_info,
                                        "session_id": prepared.session_id,
                                        "metadata": {**trace_metadata, **prepared.request_metadata},
                                    }
                                    return
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="user",
                                    text=str(chunk.get("tool_output", "")),
                                    invocation_id=prepared.invocation_id,
                                    event_type="tool_result",
                                    metadata={
                                        "tool_name": tool_name,
                                        "tool_output": chunk.get("tool_output", ""),
                                        "run_id": tool_run_id,
                                        "tool_call_id": tool_call_id,
                                        "observability": _tool_observability_metadata(
                                            tool_name, chunk.get("tool_output", "")
                                        ),
                                        "tool_receipt": _tool_receipt_metadata(
                                            session_id=prepared.session_id,
                                            run_id=tool_run_id,
                                            tool_call_id=tool_call_id,
                                            tool_name=tool_name,
                                            tool_args=tool_args,
                                            checkpoint_id=checkpoint_metadata.get("checkpoint_id"),
                                            framework=checkpoint_metadata.get("framework"),
                                            framework_ref=checkpoint_metadata.get("framework_ref"),
                                            status=(
                                                "failed"
                                                if isinstance(chunk.get("tool_output"), Mapping)
                                                and chunk.get("tool_output", {}).get("ok") is False
                                                else "completed"
                                            ),
                                        ),
                                    },
                                    session_service_provider=provider,
                                )
                                deferred_tool_names = _extract_deferred_tool_names(
                                    chunk.get("tool_output", "")
                                )
                                if tool_name == "tool_search" and deferred_tool_names:
                                    await append_deferred_tools_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        deferred_tool_names=deferred_tool_names,
                                        invocation_id=prepared.invocation_id,
                                        session_service_provider=provider,
                                        run_mode=run_mode,
                                        run_trigger=run_trigger,
                                    )
                                _governance_record_tool_result(
                                    governance, chunk.get("tool_output", "")
                                )
                                emitted_anything = True
                                await _persist_accumulated_reasoning()
                                yield {
                                    "type": "tool_result",
                                    "name": chunk.get("tool_name"),
                                    "output": chunk.get("tool_output", ""),
                                    "run_id": chunk.get("run_id"),
                                }
                                continue
                            if chunk_type in ("interrupt", "approval"):
                                await _persist_assistant_snapshot(force=True)
                                # langgraph_runner 流式路径冒 `approval`(含 HITL action_requests);
                                # invoke 路径冒 `interrupt`。两者统一走 approval_request 通道。
                                interrupt_info = chunk.get("interrupt_info")
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model",
                                    text="approval requested",
                                    invocation_id=prepared.invocation_id,
                                    event_type="approval_request",
                                    metadata={"interrupt_info": interrupt_info},
                                    session_service_provider=provider,
                                )
                                await append_run_status_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    status="interrupted",
                                    invocation_id=prepared.invocation_id,
                                    detail="approval_required",
                                    session_service_provider=provider,
                                    run_mode=run_mode,
                                    run_trigger=run_trigger,
                                )
                                emitted_anything = True
                                yield {
                                    "type": "interrupt",
                                    "interrupt_info": interrupt_info,
                                    "session_id": prepared.session_id,
                                    "metadata": {**trace_metadata, **prepared.request_metadata},
                                }
                                return
                            if chunk_type == "final":
                                saw_final_chunk = True
                                final_text = str(chunk.get("output", ""))
                                if final_text:
                                    accumulated_text = final_text
                                if isinstance(chunk.get("usage"), Mapping):
                                    stream_usage = _normalize_usage_payload(chunk.get("usage"))
                                chunk_last = (chunk.get("metadata") or {}).get("last_usage")
                                if isinstance(chunk_last, Mapping):
                                    stream_last_usage = (
                                        _normalize_usage_payload(chunk_last) or stream_usage
                                    )
                break
            except asyncio.CancelledError:
                await _persist_accumulated_reasoning()
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status="cancelled",
                    invocation_id=prepared.invocation_id,
                    detail="cancel_requested",
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                yield {
                    "type": "cancelled",
                    "session_id": prepared.session_id,
                    "metadata": {**trace_metadata, **prepared.request_metadata},
                }
                return
            except Exception as exc:
                if attempt == 0 and not emitted_anything and _is_prompt_too_long_error(exc):
                    yield {"type": "compaction", "phase": "start", "trigger": "prompt_too_long"}
                    try:
                        checkpoint = await _compact_conversation_history_with_governance(
                            governance,
                            session_id=prepared.session_id,
                            author=runner_name,
                            invocation_id=prepared.invocation_id,
                            model=model,
                            model_metadata=prepared.model_metadata,
                            force=True,
                            trigger="prompt_too_long",
                            keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                            session_service_provider=provider,
                        )
                    except RuntimeCircuitOpen as circuit_exc:
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status=_failed_status_for_resume(resume_input),
                            invocation_id=prepared.invocation_id,
                            detail=str(circuit_exc),
                            metadata={"governance": circuit_exc.metadata},
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        await _persist_accumulated_reasoning()
                        yield {"type": "error", "message": str(circuit_exc) or "Agent 运行失败"}
                        return
                    if checkpoint:
                        yield {
                            "type": "compaction",
                            "phase": "done",
                            "trigger": "prompt_too_long",
                            "compacted_until_seq_id": int(
                                (checkpoint.metadata or {}).get("compacted_until_seq_id") or 0
                            )
                            or None,
                        }
                        prepared = await _refresh_history(
                            prepared, session_service_provider=provider
                        )
                        runtime_context.history = list(prepared.history)
                        continue
                if attempt == 0 and not emitted_anything:
                    fallback_model = fallback_model_for_exception(exc, current_model=model or "")
                    if fallback_model:
                        model = fallback_model
                        prepare_runner(runner, fallback_model)
                        prepared.model_options = {
                            **prepared.model_options,
                            **model_policy_options_for_model(fallback_model),
                        }
                        runtime_context.model = fallback_model
                        runtime_context.model_options = prepared.model_options
                        _set_span_attribute(span, "ksadk.model.fallback", fallback_model)
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status="in_progress",
                            invocation_id=prepared.invocation_id,
                            detail=f"fallback_model:{fallback_model}",
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        continue
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status=_failed_status_for_resume(resume_input),
                    invocation_id=prepared.invocation_id,
                    detail=str(exc),
                    metadata=(
                        {"governance": exc.metadata}
                        if isinstance(exc, RuntimeCircuitOpen)
                        else None
                    ),
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                await _persist_accumulated_reasoning()
                yield {"type": "error", "message": str(exc) or "Agent 运行失败"}
                return

        request_metadata_without_agentengine = {
            key: value for key, value in prepared.request_metadata.items() if key != "agentengine"
        }
        assistant_metadata = {
            **trace_metadata,
            **request_metadata_without_agentengine,
            **_merge_agentengine_metadata(prepared.request_metadata, runner_agentengine_metadata),
        }
        if responses_output:
            assistant_metadata["responses_output"] = responses_output
        if stream_usage:
            assistant_metadata["usage"] = stream_usage
        if stream_last_usage:
            assistant_metadata["last_usage"] = stream_last_usage
        elif stream_usage:
            assistant_metadata["last_usage"] = stream_usage
        if responses_response_id:
            assistant_metadata["response_id"] = responses_response_id
        if emitted_anything and not saw_final_chunk and not accumulated_text:
            await append_run_status_event(
                session_id=prepared.session_id,
                author=runner_name,
                status=_failed_status_for_resume(resume_input),
                invocation_id=prepared.invocation_id,
                detail="runner_stream_ended_without_final_output",
                session_service_provider=provider,
                run_mode=run_mode,
                run_trigger=run_trigger,
            )
            await _persist_accumulated_reasoning()
            _finish_span()
            yield {
                "type": "error",
                "message": "Agent 运行流已结束，但没有返回最终输出。",
                "session_id": prepared.session_id,
                "metadata": {
                    **assistant_metadata,
                    "detail": "runner_stream_ended_without_final_output",
                },
            }
            return
        _set_conversation_output_attributes(span, accumulated_text)

        await _persist_accumulated_reasoning()
        await append_conversation_event(
            session_id=prepared.session_id,
            author=runner_name,
            role="model",
            text=accumulated_text,
            invocation_id=prepared.invocation_id,
            event_type="assistant_message",
            metadata=assistant_metadata or None,
            session_service_provider=provider,
        )
        await _update_session_metadata_after_assistant_turn(
            service=provider(),
            session_id=prepared.session_id,
            assistant_text=accumulated_text,
            model=model,
        )
        await _auto_save_ltm_turn(
            agent_id=agent_id,
            user_id=user_id,
            prepared=prepared,
            output_text=accumulated_text,
            runner_type=runtime_context.runner_type,
            model=model,
        )
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="completed",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )
        _set_conversation_usage_attributes(span, assistant_metadata.get("usage"))
        _finish_span()
        yield {
            "type": "completed",
            "output_text": accumulated_text,
            "model": model,
            "session_id": prepared.session_id,
            "metadata": assistant_metadata,
            "responses_output": responses_output,
            "response_id": responses_response_id,
            "usage": assistant_metadata.get("usage"),
        }
    finally:
        _finish_span()
