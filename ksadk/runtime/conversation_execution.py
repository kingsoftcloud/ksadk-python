"""Conversation preparation and persistence around the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from ksadk.conversations.run_kinds import RUN_MODE_FOREGROUND
from ksadk.conversations.runtime_compaction import preview_auto_compaction
from ksadk.conversations.runtime_metadata import (
    _update_session_metadata_after_assistant_turn,
)
from ksadk.conversations.runtime_persistence import append_run_status_event
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationResumed,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent
from ksadk.events.pipeline import CanonicalEventPipeline
from ksadk.events.reducer import RunProjection, StreamReducer
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    RESUME_START_REQUEST_NATIVE_KEY,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    StartRequest,
)
from ksadk.runtime.executor import RuntimeExecutor, RuntimeStartPreparation
from ksadk.runtime.factory import apply_runtime_start_request_defaults
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.sessions import resolve_session_service

_TERMINAL_EVENTS = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.canceled",
    }
)

_RUN_STATUS_BY_EVENT = {
    "run.started": "in_progress",
    "run.interrupted": "interrupted",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.canceled": "cancelled",
}


async def iter_runtime_conversation_events(
    *,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    agent_id: str,
    user_id: str,
    messages: Sequence[dict[str, Any]],
    session_id: str | None,
    model: str | None,
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: dict[str, Any] | None = None,
    instructions: str | None = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    response_id: str | None = None,
    account_id: str | None = None,
    invocation_id: str | None = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
    runtime_preparation: RuntimeStartPreparation | None = None,
    _execution_context: dict[str, str] | None = None,
) -> AsyncIterator[RuntimeEvent]:
    """Prepare once, execute through RuntimeExecutor, and persist RuntimeEvents."""

    turn_started_monotonic = time.monotonic()
    provider = session_service_provider or resolve_session_service
    compaction_preview = await preview_auto_compaction(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        model_metadata=model_metadata,
        session_service_provider=provider,
    )
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
        session_service_provider=provider,
        run_mode=run_mode,
        runtime_type=launch_context.runtime_type,
    )
    if _execution_context is not None:
        _execution_context["session_id"] = prepared.session_id
    canonical_messages = prepared.responses_history or [dict(item) for item in messages]
    conversation_request = {
        "messages": canonical_messages,
        "model_metadata": prepared.model_metadata,
        "model_options": prepared.model_options,
        "state_delta": dict(state_delta or {}),
        "instructions": prepared.instructions,
        "request_metadata": prepared.request_metadata,
        "custom_metadata": dict(custom_metadata or {}),
        "account_id": account_id,
        "response_id": response_id,
        "prepared_turn": asdict(prepared),
    }
    native_session_metadata = await _session_native_metadata(
        runtime_type=launch_context.runtime_type,
        session_id=prepared.session_id,
        session_service_provider=provider,
    )
    request = apply_runtime_start_request_defaults(
        launch_context,
        StartRequest(
            input=prepared.user_input,
            user_id=user_id,
            session_id=prepared.session_id,
            agent_id=agent_id,
            model=model,
            metadata={
                "invocation_id": prepared.invocation_id,
                CONVERSATION_PREPROCESSING_METADATA_KEY: conversation_request,
                **native_session_metadata,
            },
        ),
    )
    checkpoint_resume = _checkpoint_resume_input(prepared.resume_input)
    if checkpoint_resume is None:
        handle = await executor.start(
            launch_context,
            request,
            preparation=runtime_preparation,
        )
    else:
        handle = await _resume_runtime_handle(
            executor=executor,
            launch_context=launch_context,
            session_id=prepared.session_id,
            resume_input=checkpoint_resume,
            request=request,
        )
        request = request.model_copy(
            update={
                "metadata": {
                    **request.metadata,
                    "invocation_id": handle.run_id,
                }
            }
        )
    store = RuntimeEventStore(provider())
    pipeline = CanonicalEventPipeline(store, session_id=prepared.session_id)
    terminal = False
    interrupted = False
    completed_assistant_text = ""
    usage: dict[str, int] | None = None
    try:
        for context_event in _compaction_runtime_events(
            prepared=prepared,
            preview=compaction_preview,
        ):
            for persisted in await pipeline.ingest(context_event):
                yield persisted
        async for event in executor.stream(handle):
            _validate_event_scope(event, request)
            for persisted in await pipeline.ingest(event):
                await _project_runtime_run_status(
                    persisted,
                    session_id=prepared.session_id,
                    author=agent_id,
                    run_mode=prepared.run_mode,
                    run_trigger=prepared.run_trigger,
                    session_service_provider=provider,
                )
                terminal = persisted.event_type in _TERMINAL_EVENTS
                interrupted = persisted.event_type == "run.interrupted"
                if isinstance(persisted, RunCompleted):
                    completed_assistant_text = _selected_output_text(
                        pipeline.reducer.snapshot(), persisted
                    )
                elif isinstance(persisted, UsageReported):
                    usage = {
                        "input_tokens": persisted.input_tokens,
                        "output_tokens": persisted.output_tokens,
                        "total_tokens": persisted.total_tokens,
                        "cached_tokens": persisted.cached_tokens,
                        "reasoning_tokens": persisted.reasoning_tokens,
                    }
                yield persisted
        if not terminal and not interrupted:
            raise RuntimeError("runtime stream ended without a terminal or interrupted event")
    except asyncio.CancelledError:
        cancel_result = await executor.cancel(handle)
        cancelled_event = RunCanceled(
            schema_version=2,
            event_id=f"cancel:{handle.run_id}:{cancel_result.value}",
            seq=0,
            timestamp=time.time(),
            run_id=handle.run_id,
            scope_id=handle.run_id,
            source=SourceRef(
                framework="ksadk", metadata={"cancel_result": cancel_result.value}
            ),
            status="canceled",
            reason=cancel_result.value,
        )
        persisted_cancelled = (await pipeline.ingest(cancelled_event))[-1]
        await _project_runtime_run_status(
            persisted_cancelled,
            session_id=prepared.session_id,
            author=agent_id,
            run_mode=prepared.run_mode,
            run_trigger=prepared.run_trigger,
            session_service_provider=provider,
        )
        await executor.close(handle)
        raise
    except BaseException:
        if not interrupted:
            await executor.close(handle)
        raise
    else:
        if terminal and completed_assistant_text:
            await _update_session_metadata_after_assistant_turn(
                service=provider(),
                session_id=prepared.session_id,
                assistant_text=completed_assistant_text,
                model=model,
            )
        if terminal:
            _record_canonical_baseline_turn(
                prepared=prepared,
                model=model,
                usage=usage,
                turn_started_monotonic=turn_started_monotonic,
            )
        if terminal:
            await executor.close(handle)


async def _session_native_metadata(
    *,
    runtime_type: str,
    session_id: str,
    session_service_provider: Callable[[], Any],
) -> dict[str, str]:
    """Resolve a provider-native continuation from the canonical Session log.

    The HTTP conversation path creates a fresh adapter transport for every
    terminal turn. Codex therefore needs the prior native thread id on the
    next ``StartRequest``; otherwise one AgentEngine Session becomes unrelated
    one-turn Codex threads.
    """

    if str(runtime_type or "").strip().lower() != "codex":
        return {}
    events = await RuntimeEventStore(session_service_provider()).list(
        session_id,
        limit=512,
    )
    for event in reversed(events):
        if not isinstance(event, (ContinuationCreated, ContinuationResumed)):
            continue
        if event.continuation_kind != "thread_resume":
            continue
        ref = getattr(event, "ref", None)
        thread_id = (
            str(ref.get("thread_id") or "").strip()
            if isinstance(ref, Mapping)
            else ""
        )
        if not thread_id:
            thread_id = str(event.source.metadata.get("thread_id") or "").strip()
        if thread_id:
            return {"thread_id": thread_id}
    return {}


def _compaction_runtime_events(
    *,
    prepared: Any,
    preview: Any,
) -> list[RuntimeEvent]:
    if not prepared.compaction_triggered:
        return []
    trigger = str(prepared.compaction_trigger or "auto")
    source = SourceRef(
        framework="ksadk",
        metadata={
            "total_chars": preview.total_chars,
            "total_estimated_tokens": preview.total_estimated_tokens,
            "group_count": preview.group_count,
            "threshold_percentage": preview.auto_compact_threshold_percentage,
        },
    )
    common = {
        "schema_version": 2,
        "seq": 0,
        "timestamp": time.time(),
        "run_id": prepared.invocation_id,
        "scope_id": prepared.invocation_id,
        "source": source,
        "trigger": trigger,
    }
    return [
        ContextCompactionStarted(
            event_id=f"compaction-start:{prepared.invocation_id}",
            **common,
        ),
        ContextCompactionCompleted(
            event_id=f"compaction-completed:{prepared.invocation_id}",
            compacted_until_seq=int(prepared.compacted_until_seq_id or 0),
            **common,
        ),
    ]


def _record_canonical_baseline_turn(
    *,
    prepared: Any,
    model: str | None,
    usage: Mapping[str, int] | None,
    turn_started_monotonic: float,
) -> None:
    """Keep v2 execution on the same env-gated measurement path as legacy runs."""

    from ksadk.context_engine.baseline import record_baseline_turn

    record_baseline_turn(
        getattr(prepared, "shadow_context_plan", None),
        session_id=prepared.session_id,
        invocation_id=prepared.invocation_id,
        model=str(model or ""),
        usage=usage,
        compaction_triggered=bool(getattr(prepared, "compaction_triggered", False)),
        compaction_trigger=str(getattr(prepared, "compaction_trigger", "") or ""),
        turn_latency_ms=int((time.monotonic() - turn_started_monotonic) * 1000),
    )


async def _project_runtime_run_status(
    event: RuntimeEvent,
    *,
    session_id: str,
    author: str,
    run_mode: str,
    run_trigger: str,
    session_service_provider: Callable[[], Any],
) -> None:
    """Project canonical lifecycle events into the session's active-run state."""

    status = _RUN_STATUS_BY_EVENT.get(event.event_type)
    if status is None:
        return
    detail = event.error.message if isinstance(event, RunFailed) else None
    await append_run_status_event(
        session_id=session_id,
        author=author,
        status=status,
        invocation_id=event.run_id,
        detail=str(detail) if detail else None,
        metadata={
            "runtime_event_id": event.event_id,
            "runtime_event_type": event.event_type,
        },
        session_service_provider=session_service_provider,
        run_mode=run_mode,
        run_trigger=run_trigger,
    )


def _checkpoint_resume_input(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if str(value.get("type") or "").strip() != "agentengine.resume_checkpoint":
        return None
    return dict(value)


async def _resume_runtime_handle(
    *,
    executor: RuntimeExecutor,
    launch_context: RuntimeLaunchContext,
    session_id: str,
    resume_input: Mapping[str, Any],
    request: StartRequest,
) -> RunHandle:
    run_id = str(resume_input.get("run_id") or "").strip()
    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if not run_id or not checkpoint_id:
        raise ValueError("checkpoint resume requires run_id and checkpoint_id")

    handle = executor.find_handle(launch_context.runtime_type, run_id, session_id)
    if handle is None:
        native_ref = _persisted_resume_native_ref(resume_input)
        native_ref.update({"agent_id": request.agent_id, "user_id": request.user_id})
        handle = RunHandle(
            run_id=run_id,
            session_id=session_id,
            runtime_type=launch_context.runtime_type,
            native_ref=native_ref,
        )
        handle.native_ref[RESUME_START_REQUEST_NATIVE_KEY] = request.model_dump(mode="python")
        handle = await executor.attach(launch_context, handle)
    else:
        handle.native_ref[RESUME_START_REQUEST_NATIVE_KEY] = request.model_dump(mode="python")

    target = _resume_target(resume_input)
    instruction = str(resume_input.get("resume_instruction") or "").strip()
    payload = (
        ResumePayload(kind="free_text", data=instruction)
        if resume_input.get("resume_instruction_enabled") and instruction
        else None
    )
    return await executor.resume(handle, target, payload)


def _resume_target(resume_input: Mapping[str, Any]) -> ResumeTarget:
    framework = str(resume_input.get("framework") or "").strip().lower()
    framework_ref = resume_input.get("framework_ref")
    raw_runtime_ref = framework_ref.get(framework) if isinstance(framework_ref, Mapping) else None
    runtime_ref: Mapping[str, Any] = (
        raw_runtime_ref if isinstance(raw_runtime_ref, Mapping) else {}
    )
    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    run_id = str(resume_input.get("run_id") or "").strip()
    if framework == "langgraph":
        return ResumeTarget(kind="checkpoint_id", id=checkpoint_id)
    if framework == "codex":
        thread_id = str(runtime_ref.get("thread_id") or runtime_ref.get("resume_thread_id") or "")
        return ResumeTarget(kind="thread_id", id=thread_id or run_id)
    invocation_id = str(runtime_ref.get("invocation_id") or run_id)
    return ResumeTarget(kind="invocation_id", id=invocation_id)


def _persisted_resume_native_ref(resume_input: Mapping[str, Any]) -> dict[str, Any]:
    framework = str(resume_input.get("framework") or "").strip().lower()
    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    framework_ref = resume_input.get("framework_ref")
    normalized_ref = dict(framework_ref) if isinstance(framework_ref, Mapping) else {}
    runtime_ref = normalized_ref.get(framework)
    native_ref = dict(runtime_ref) if isinstance(runtime_ref, Mapping) else {}
    native_ref["framework_ref"] = normalized_ref
    if checkpoint_id:
        native_ref.setdefault("checkpoint_id", checkpoint_id)
        native_ref.setdefault("known_checkpoint_ids", [checkpoint_id])
    return native_ref


def _validate_event_scope(event: RuntimeEvent, request: StartRequest) -> None:
    expected_run_id = str(request.metadata["invocation_id"])
    if event.schema_version != 2 or event.run_id != expected_run_id:
        raise ValueError(
            "runtime event scope does not match request: "
            f"expected run_id={expected_run_id!r}, got {event.run_id!r}"
        )


async def iter_runtime_conversation_semantic_events(
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Project canonical RuntimeEvents into the transport-neutral serializer input."""

    reducer = StreamReducer()
    approval: dict[str, Any] | None = None
    execution_context: dict[str, str] = {}
    async for event in iter_runtime_conversation_events(
        **kwargs, _execution_context=execution_context
    ):
        patch = reducer.apply(event)
        if not patch.applied:
            continue
        projection = reducer.snapshot()
        if isinstance(event, RunStarted):
            yield {
                "type": "started",
                "session_id": execution_context.get("session_id", ""),
                "metadata": dict(event.source.metadata),
            }
        elif isinstance(event, (ContextCompactionStarted, ContextCompactionCompleted)):
            semantic = {"type": "compaction", "trigger": event.trigger}
            # Distinguish start vs done for SSE projection (response.compaction.start/done)
            if isinstance(event, ContextCompactionStarted):
                semantic["phase"] = "start"
            else:
                semantic["phase"] = "done"
                semantic["compacted_until_seq_id"] = event.compacted_until_seq
            yield semantic
        elif isinstance(event, ItemUpdated) and isinstance(event.update, TextContent):
            item = next(
                candidate
                for candidate in projection.items
                if candidate.scope_id == event.scope_id and candidate.item_id == event.item_id
            )
            semantic_type = (
                "thinking"
                if item.item_kind == "reasoning" or item.phase == "commentary"
                else "text"
            )
            semantic: dict[str, Any] = {
                "type": semantic_type,
                "delta": event.update.text,
                "scope_id": event.scope_id,
                "item_id": event.item_id,
                "part_id": event.update.part_id,
                "operation": event.op,
            }
            if event.op == "replace":
                semantic["replace"] = True
            yield semantic
        elif isinstance(event, ItemStarted) and event.initial is not None:
            for part in event.initial.parts:
                if isinstance(part, ToolCallContent):
                    yield _tool_call_semantic(part)
        elif isinstance(event, ItemCompleted):
            for part in event.snapshot.parts:
                if isinstance(part, ToolCallContent):
                    yield _tool_call_semantic(part)
                elif isinstance(part, ToolResultContent):
                    yield {
                        "type": "tool_result",
                        "name": "",
                        "output": part.result,
                        "run_id": part.call_id,
                    }
        elif isinstance(event, InteractionRequested):
            if event.interaction_kind == "approval" and event.request.request_type == "approval":
                detail = event.request.detail
                approval = dict(detail) if isinstance(detail, Mapping) else {}
                approval.setdefault("approval_request_id", event.interaction_id)
                approval.setdefault("id", event.interaction_id)
                approval.setdefault("call_id", event.request.call_id)
                approval.setdefault("kind", event.request.kind)
        elif isinstance(event, RunInterrupted):
            yield {
                "type": "interrupt",
                "interrupt_info": approval
                or {
                    "reason": event.reason,
                    "interaction_id": event.interaction_id,
                    "continuation_id": event.continuation_id,
                },
                "session_id": execution_context.get("session_id", ""),
            }
        elif isinstance(event, RunFailed):
            yield {
                "type": "error",
                "message": event.error.message or "Agent 运行失败",
                "session_id": execution_context.get("session_id", ""),
                "usage": projection.usage.model_dump(),
            }
        elif isinstance(event, RunCanceled):
            yield {
                "type": "cancelled",
                "session_id": execution_context.get("session_id", ""),
                "usage": projection.usage.model_dump(),
            }
        elif isinstance(event, RunCompleted):
            completion_metadata = dict(event.source.metadata)
            request_metadata = kwargs.get("request_metadata")
            requested_agentengine = (
                request_metadata.get("agentengine")
                if isinstance(request_metadata, Mapping)
                else None
            )
            if isinstance(requested_agentengine, Mapping):
                completion_metadata["agentengine"] = dict(requested_agentengine)
            completion_metadata["runtime"] = {
                "duration_ms": event.source.metadata.get("duration_ms"),
                "runtime_type": kwargs["launch_context"].runtime_type,
            }
            yield {
                "type": "completed",
                "output_text": _selected_output_text(projection, event),
                "session_id": execution_context.get("session_id", ""),
                "usage": projection.usage.model_dump(),
                "metadata": completion_metadata,
            }


def _tool_call_semantic(part: ToolCallContent) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "name": part.name,
        "args": part.arguments if isinstance(part.arguments, dict) else {},
        "run_id": part.call_id,
    }


def _selected_output_text(projection: RunProjection, completed: RunCompleted) -> str:
    """Resolve final text exclusively through authoritative run output refs."""

    chunks: list[str] = []
    for ref in completed.output_refs:
        item = next(
            (
                candidate
                for candidate in projection.items
                if candidate.scope_id == ref.scope_id and candidate.item_id == ref.item_id
            ),
            None,
        )
        if item is None:
            continue
        for part in item.parts:
            if isinstance(part, TextContent) and (
                ref.part_id is None or ref.part_id == part.part_id
            ):
                chunks.append(part.text)
    return "".join(chunks)


async def invoke_runtime_conversation_once(
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Collect one RuntimeExecutor turn for non-streaming protocol responses."""

    session_id = str(kwargs.get("session_id") or "")
    output_text = ""
    usage: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    completed = False
    async for event in iter_runtime_conversation_semantic_events(**kwargs):
        event_type = event.get("type")
        if event_type == "started":
            session_id = str(event.get("session_id") or session_id)
        elif event_type == "text":
            delta = str(event.get("delta") or "")
            output_text = delta if event.get("replace") else output_text + delta
        elif event_type == "completed":
            completed = True
            session_id = str(event.get("session_id") or session_id)
            output_text = str(event.get("output_text") or output_text)
            usage = dict(event.get("usage") or {})
            metadata = dict(event.get("metadata") or {})
        elif event_type == "error":
            raise RuntimeError(str(event.get("message") or "Agent 运行失败"))
        elif event_type == "cancelled":
            raise asyncio.CancelledError
        elif event_type == "interrupt":
            raise RuntimeError("runtime input is required before this run can complete")
    if not completed:
        raise RuntimeError("runtime did not produce a completed result")
    return session_id, {
        "output_text": output_text,
        "usage": usage,
        "metadata": metadata,
    }


__all__ = [
    "invoke_runtime_conversation_once",
    "iter_runtime_conversation_events",
    "iter_runtime_conversation_semantic_events",
]
