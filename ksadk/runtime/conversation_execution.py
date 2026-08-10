"""Conversation preparation and persistence around the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
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
from ksadk.events.runtime_event import EventType, RuntimeEvent
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
from ksadk.runtime.launch import RuntimeLaunchContext
from ksadk.sessions import resolve_session_service

_TERMINAL_EVENTS = frozenset(
    {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELED,
    }
)

_RUN_STATUS_BY_EVENT = {
    EventType.RUN_STARTED: "in_progress",
    EventType.RUN_INTERRUPTED: "interrupted",
    EventType.RUN_COMPLETED: "completed",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELED: "cancelled",
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
) -> AsyncIterator[RuntimeEvent]:
    """Prepare once, execute through RuntimeExecutor, and persist RuntimeEvents."""

    provider = session_service_provider or resolve_session_service
    prompt_config = _resolve_runtime_prompt_config(launch_context.config)
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
        deployment_mode=getattr(launch_context, "deployment_mode", "local"),
        agent_system=prompt_config["agent_system"],
        agent_task=prompt_config["agent_task"],
        prompt_integration_mode=prompt_config["prompt_integration_mode"],
    )
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
    request = StartRequest(
        input=prepared.user_input,
        user_id=user_id,
        session_id=prepared.session_id,
        agent_id=agent_id,
        model=model,
        config=dict(launch_context.config),
        metadata={
            "invocation_id": prepared.invocation_id,
            CONVERSATION_PREPROCESSING_METADATA_KEY: conversation_request,
        },
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
    terminal = False
    interrupted = False
    completed_assistant_text = ""
    _baseline_turn_start = _baseline_monotonic()
    _baseline_usage: dict[str, Any] = {}
    _baseline_ptl = False
    _baseline_attempts = 0
    try:
        for context_event in _compaction_runtime_events(
            prepared=prepared,
            preview=compaction_preview,
            agent_id=agent_id,
            user_id=user_id,
        ):
            yield await store.append_one(context_event)
        async for event in executor.stream(handle):
            _validate_event_scope(event, request)
            persisted = await store.append_one(event)
            await _project_runtime_run_status(
                persisted,
                run_mode=prepared.run_mode,
                run_trigger=prepared.run_trigger,
                session_service_provider=provider,
            )
            if (
                persisted.event_type == EventType.TEXT_COMPLETED
                and persisted.phase == "final_answer"
            ):
                completed_assistant_text = str(persisted.payload.get("text") or "")
                usage_payload = persisted.payload.get("usage")
                if isinstance(usage_payload, Mapping):
                    _baseline_usage = dict(usage_payload)
            terminal = persisted.event_type in _TERMINAL_EVENTS
            interrupted = persisted.event_type == EventType.RUN_INTERRUPTED
            yield persisted
        if not terminal and not interrupted:
            raise RuntimeError("runtime stream ended without a terminal or interrupted event")
    except asyncio.CancelledError:
        cancel_result = await executor.cancel(handle)
        cancelled_event = RuntimeEvent.create(
            EventType.RUN_CANCELED,
            agent_id=str(request.agent_id or ""),
            user_id=request.user_id,
            session_id=request.session_id,
            invocation_id=str(request.metadata["invocation_id"]),
            seq_id=0,
            payload={
                "status": "cancelled",
                "cancel_result": cancel_result.value,
            },
        )
        persisted_cancelled = await store.append_one(cancelled_event)
        await _project_runtime_run_status(
            persisted_cancelled,
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
            await executor.close(handle)

    _record_baseline_turn(
        prepared=prepared,
        model=model,
        usage=_baseline_usage,
        ptl=_baseline_ptl,
        attempts=_baseline_attempts,
        turn_start_monotonic=_baseline_turn_start,
    )
    # PR E：把 runtime usage 回填进真实 ContextPlan + 压缩后 Memory Candidate 抽取（方案 §11.1
    # 步骤 14-16）。仅 hosted 路径（context_plan 非空）执行；失败不阻断主链路。
    await _finalize_hosted_turn(
        prepared=prepared,
        usage=_baseline_usage,
        session_service_provider=provider,
    )


async def _finalize_hosted_turn(
    *,
    prepared: Any,
    usage: Mapping[str, Any] | None,
    session_service_provider: Callable[[], Any],
) -> None:
    """PR E：hosted turn 收尾——委托共享 HostedTurnFinalizer（方案 §11.1 / P0 收敛）。

    Studio 与 canonical Runtime 共用同一收尾逻辑，避免两条路径漂移。
    """
    from ksadk.runtime.hosted_finalizer import FinalizeContext, finalize_hosted_turn

    await finalize_hosted_turn(
        FinalizeContext(
            session_id=getattr(prepared, "session_id", ""),
            invocation_id=getattr(prepared, "invocation_id", ""),
            user_id=getattr(prepared, "user_id", "") or "local-user",
            context_plan=getattr(prepared, "context_plan", None),
            shadow_context_plan=getattr(prepared, "shadow_context_plan", None),
            usage=usage,
            runtime_type=str(
                (getattr(prepared, "shadow_context_plan", None) or {}).get("runtime_type") or ""
            ),
            prompt_integration_mode=str(getattr(prepared, "prompt_integration_mode", "")),
        ),
        session_service_provider=session_service_provider,
    )


def _resolve_runtime_prompt_config(config: Mapping[str, Any] | None) -> dict[str, str]:
    """Resolve the per-build Prompt ownership contract from ``agentengine.yaml``.

    Standard Code/Container deployments only carry ``RuntimeLaunchContext.config``;
    without this projection the Studio path can enable hosted Context while the same
    immutable build silently falls back after cloud deployment.  Prefer the nested
    ``context`` block, while accepting the existing flat request-config keys.
    """

    raw = dict(config or {})
    nested_value = raw.get("context")
    nested = dict(nested_value) if isinstance(nested_value, Mapping) else {}
    ownership = (
        str(
            nested.get("prompt_ownership")
            or nested.get("promptOwnership")
            or raw.get("prompt_ownership")
            or raw.get("promptOwnership")
            or ""
        )
        .strip()
        .lower()
    )
    nested_mode = nested.get("integration_mode") or nested.get("integrationMode")
    if nested_mode:
        mode = str(nested_mode).strip().lower()
    elif ownership == "ksadk":
        # A nested ownership declaration is the build contract and must not be
        # silently weakened by a legacy flat request setting.
        mode = "ksadk_hosted"
    else:
        mode = str(raw.get("prompt_integration_mode") or "").strip().lower()
    if mode not in {"", "ksadk_hosted", "framework_assisted", "native_runtime"}:
        mode = ""

    return {
        "agent_system": str(
            nested.get("agent_system")
            or nested.get("agentSystem")
            or raw.get("agent_system")
            or raw.get("base_instructions")
            or raw.get("prompt")
            or ""
        ),
        "agent_task": str(
            nested.get("agent_task") or nested.get("agentTask") or raw.get("agent_task") or ""
        ),
        "prompt_integration_mode": mode,
    }


def _baseline_monotonic() -> float:
    import time

    return time.monotonic()


def _record_baseline_turn(
    *,
    prepared: Any,
    model: str | None,
    usage: Mapping[str, Any] | None,
    ptl: bool,
    attempts: int,
    turn_start_monotonic: float | None,
) -> None:
    """env-gated 旁路采集：未启用时 no-op，启用时记录一条 turn 基线，不进决策路径、不抛异常。"""
    import time

    from ksadk.context_engine.baseline import record_baseline_turn

    latency_ms = None
    if turn_start_monotonic is not None:
        latency_ms = int((time.monotonic() - turn_start_monotonic) * 1000)
    record_baseline_turn(
        getattr(prepared, "shadow_context_plan", None),
        session_id=getattr(prepared, "session_id", ""),
        invocation_id=getattr(prepared, "invocation_id", ""),
        model=str(model or ""),
        usage=usage,
        compaction_triggered=bool(getattr(prepared, "compaction_triggered", False)),
        compaction_trigger=str(getattr(prepared, "compaction_trigger", "") or ""),
        prompt_too_long=ptl,
        retry_attempts=attempts,
        turn_latency_ms=latency_ms,
    )


def _compaction_runtime_events(
    *,
    prepared: Any,
    preview: Any,
    agent_id: str,
    user_id: str,
) -> list[RuntimeEvent]:
    if not prepared.compaction_triggered:
        return []
    trigger = str(prepared.compaction_trigger or "auto")
    scope = {
        "agent_id": agent_id,
        "user_id": user_id,
        "session_id": prepared.session_id,
        "invocation_id": prepared.invocation_id,
    }
    preview_payload = {
        "phase": "start",
        "trigger": trigger,
        "total_chars": preview.total_chars,
        "total_estimated_tokens": preview.total_estimated_tokens,
        "group_count": preview.group_count,
        "threshold_percentage": preview.auto_compact_threshold_percentage,
    }
    completed_payload = {
        **preview_payload,
        "phase": "done",
        "compacted_until_seq_id": int(prepared.compacted_until_seq_id or 0),
    }
    return [
        RuntimeEvent.create(
            EventType.CONTEXT_COMPACTION_STARTED,
            seq_id=0,
            payload=preview_payload,
            **scope,
        ),
        RuntimeEvent.create(
            EventType.CONTEXT_COMPACTION_COMPLETED,
            seq_id=0,
            payload=completed_payload,
            **scope,
        ),
    ]


async def _project_runtime_run_status(
    event: RuntimeEvent,
    *,
    run_mode: str,
    run_trigger: str,
    session_service_provider: Callable[[], Any],
) -> None:
    """Project canonical lifecycle events into the session's active-run state."""

    status = _RUN_STATUS_BY_EVENT.get(event.event_type)
    if status is None:
        return
    detail = event.payload.get("error") or event.payload.get("detail")
    await append_run_status_event(
        session_id=event.session_id,
        author=event.agent_id,
        status=status,
        invocation_id=event.invocation_id,
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
    runtime_ref: Mapping[str, Any] = raw_runtime_ref if isinstance(raw_runtime_ref, Mapping) else {}
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
    expected = {
        "agent_id": str(request.agent_id or ""),
        "user_id": request.user_id,
        "session_id": request.session_id,
        "invocation_id": str(request.metadata["invocation_id"]),
    }
    mismatches = {
        field: (expected_value, getattr(event, field))
        for field, expected_value in expected.items()
        if getattr(event, field) != expected_value
    }
    if mismatches:
        raise ValueError(f"runtime event scope does not match request: {mismatches!r}")


async def iter_runtime_conversation_semantic_events(
    **kwargs: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Project canonical RuntimeEvents into the transport-neutral serializer input."""

    accumulated_text = ""
    usage: dict[str, Any] = {}
    approval: dict[str, Any] | None = None
    async for event in iter_runtime_conversation_events(**kwargs):
        payload = event.payload
        event_type = event.event_type
        if event_type == EventType.RUN_STARTED:
            yield {
                "type": "started",
                "session_id": event.session_id,
                "metadata": dict(payload.get("metadata") or {}),
            }
        elif event_type in {
            EventType.CONTEXT_COMPACTION_STARTED,
            EventType.CONTEXT_COMPACTION_COMPLETED,
        }:
            yield {
                "type": "compaction",
                **dict(payload),
            }
        elif event_type == EventType.TEXT_DELTA:
            delta = str(payload.get("text") or "")
            replace = bool(payload.get("replace"))
            accumulated_text = delta if replace else accumulated_text + delta
            semantic: dict[str, Any] = {"type": "text", "delta": delta}
            if replace:
                semantic["replace"] = True
            yield semantic
        elif event_type == EventType.TEXT_COMPLETED:
            snapshot = str(payload.get("text") or "")
            if snapshot != accumulated_text:
                if snapshot.startswith(accumulated_text):
                    delta = snapshot[len(accumulated_text) :]
                    if delta:
                        yield {"type": "text", "delta": delta}
                else:
                    yield {"type": "text", "delta": snapshot, "replace": True}
            accumulated_text = snapshot
        elif event_type == EventType.REASONING_DELTA:
            yield {"type": "thinking", "delta": str(payload.get("text") or "")}
        elif event_type == EventType.TOOL_CALL_BEGIN:
            yield {
                "type": "tool_call",
                "name": payload.get("name"),
                "args": dict(payload.get("args") or {}),
                "run_id": payload.get("call_id"),
                "stage": payload.get("stage"),
                "event_kind": payload.get("event_kind"),
                "display_title": payload.get("display_title"),
                "display_summary": payload.get("display_summary"),
            }
        elif event_type == EventType.TOOL_CALL_END:
            yield {
                "type": "tool_result",
                "name": payload.get("name"),
                "output": payload.get("result", payload.get("error", "")),
                "run_id": payload.get("call_id"),
            }
        elif event_type == EventType.APPROVAL_REQUESTED:
            detail = payload.get("detail")
            approval = dict(detail) if isinstance(detail, Mapping) else {}
            approval.setdefault("approval_request_id", payload.get("approval_id"))
            approval.setdefault("id", payload.get("approval_id"))
            approval.setdefault("call_id", payload.get("call_id"))
            # ``kind=tool`` only describes the interrupt category.  It is not
            # a concrete tool name: inventing one would serialize a generic
            # interrupt as an MCP approval request and lose the extension
            # event that clients use to render a human-input prompt.
            if payload.get("tool_name"):
                approval.setdefault("tool_name", payload.get("tool_name"))
        elif event_type == EventType.USAGE_REPORTED:
            usage = dict(payload)
        elif event_type == EventType.RUN_INTERRUPTED:
            yield {
                "type": "interrupt",
                "interrupt_info": approval or dict(payload),
                "session_id": event.session_id,
            }
        elif event_type == EventType.RUN_FAILED:
            yield {
                "type": "error",
                "message": str(payload.get("error") or "Agent 运行失败"),
                "session_id": event.session_id,
                "usage": usage,
            }
        elif event_type == EventType.RUN_CANCELED:
            yield {
                "type": "cancelled",
                "session_id": event.session_id,
                "usage": usage,
            }
        elif event_type == EventType.RUN_COMPLETED:
            raw_completion_metadata = payload.get("metadata")
            completion_metadata = (
                dict(raw_completion_metadata)
                if isinstance(raw_completion_metadata, Mapping)
                else {}
            )
            request_metadata = kwargs.get("request_metadata")
            requested_agentengine = (
                request_metadata.get("agentengine")
                if isinstance(request_metadata, Mapping)
                else None
            )
            if isinstance(requested_agentengine, Mapping):
                completion_metadata["agentengine"] = dict(requested_agentengine)
            if isinstance(payload.get("agentengine"), Mapping):
                completion_metadata["agentengine"] = dict(payload["agentengine"])
            completion_metadata["runtime"] = {
                "duration_ms": payload.get("duration_ms"),
                "runtime_type": kwargs["launch_context"].runtime_type,
            }
            yield {
                "type": "completed",
                "output_text": accumulated_text,
                "session_id": event.session_id,
                "usage": usage,
                "metadata": completion_metadata,
            }


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
