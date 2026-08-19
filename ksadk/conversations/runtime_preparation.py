from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ksadk.context_engine.shadow_plan import (
    build_shadow_context_plan_dict,
    minimal_shadow_context_plan_dict,
)
from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.context import (
    build_history_from_events,
    build_request_history,
    build_responses_history_from_messages,
    canonical_event_type,
    project_responses_history,
)
from ksadk.conversations.model_options import normalize_model_options
from ksadk.conversations.normalize import (
    compact_attachment_for_session,
)
from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    RUN_TRIGGER_APPROVAL_RESUME,
    RUN_TRIGGER_CHECKPOINT_RESUME,
    trigger_from_resume_input,
    validate_run_mode,
)
from ksadk.conversations.runtime_governance import (
    RuntimeGovernanceState,
    _compact_conversation_history_with_governance,
    _governance_record_approval_response,
)
from ksadk.conversations.runtime_input import (
    _merge_request_history_with_session_history,
    _merge_responses_history_with_session_history,
)
from ksadk.conversations.runtime_metadata import (
    _build_attachment_context_state_delta,
    _canonical_input_messages,
    _latest_user_turn,
    _normalized_conversation_messages,
    _parts_include_file,
    _resolve_effective_attachment_context,
    _resolve_runtime_model_metadata,
    _update_session_metadata_after_user_turn,
    _user_event_content,
)
from ksadk.conversations.runtime_observability import _latest_deferred_tool_names
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.conversations.runtime_persistence import (
    append_conversation_event,
    append_run_resume_event,
    append_run_status_event,
    ensure_conversation_session,
)
from ksadk.conversations.runtime_resume import (
    _agentengine_resume_metadata,
    _approval_decision_from_resume,
    _approval_resume_run_mode,
    _consecutive_approval_denials_from_events,
    _execute_approved_builtin_tool_resume,
    _find_tool_receipt_event_by_key,
    _format_resume_response_text,
    _has_pending_approval,
    _is_approval_resume_input,
    _is_checkpoint_resume_input,
    _normalize_approval_resume_input,
    _normalize_checkpoint_resume_input,
    _tool_receipt_idempotency_key_for_resume,
)
from ksadk.ids import new_run_id
from ksadk.model_policy import model_policy_options_for_model
from ksadk.sessions import SessionEvent, resolve_session_service

logger = logging.getLogger(__name__)


async def build_run_input(
    *,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    invocation_id: Optional[str] = None,
    governance_state: RuntimeGovernanceState | None = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
    runner: Any | None = None,
    runtime_type: str | None = None,
    agent_system: str = "",
    agent_task: str = "",
    prompt_integration_mode: str = "",
    context_engine_rollout: str | None = None,
    memory_recall_enabled: bool | None = None,
    memory_write_rollout: str | None = None,
    memory_enabled: bool | None = None,
    memory_write_mode: str = "candidate",
    flush_before_compaction: bool = True,
    provider_ref: str = "local-default",
    deployment_mode: str = "local",
    agent_max_input_tokens: int | None = None,
    agent_reserve_output_tokens: int | None = None,
) -> PreparedConversationTurn:
    """构建一次 turn 的标准运行输入，并在进入模型前做上下文投影/压缩。

    run_mode 由 caller 按 endpoint 语义传入（Background:true→background，普通→foreground）；
    run_trigger 由 resume_input 推导（new_run/checkpoint_resume/approval_resume）。
    """
    caller_run_mode = validate_run_mode(run_mode)
    caller_run_trigger = trigger_from_resume_input(resume_input)
    provider = session_service_provider or resolve_session_service
    service = provider()
    resolved_user_id = user_id
    if session_id:
        existing_session = await service.get_session(session_id)
        if existing_session and existing_session.user_id:
            resolved_user_id = existing_session.user_id

    session = await ensure_conversation_session(
        agent_id=agent_id,
        user_id=resolved_user_id,
        session_id=session_id,
        session_service_provider=provider,
    )
    resolved_session_id = session.id
    resolved_invocation_id = str(invocation_id or new_run_id(resolved_session_id))
    resolved_model_metadata = await _resolve_runtime_model_metadata(
        model,
        model_metadata=model_metadata,
    )
    normalized_request_metadata = dict(request_metadata or {})
    normalized_custom_metadata = dict(custom_metadata or {})
    policy_model = model or os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
    normalized_model_options = {
        **normalize_model_options(model_options),
        **model_policy_options_for_model(policy_model or ""),
    }
    normalized_instructions = str(instructions or "").strip()

    # PR A：当 agent_system/agent_task 非空时，编译真实 CompiledPrompt（含 stable section）。
    # 仅用于 hash/trace/future projection，不改 Runner 输入（payload["instructions"] 不变）。
    # platform_policy_source 默认 EnvPlatformPolicySource（env 未设→不产 platform_safety）。
    compiled_prompt: dict[str, Any] | None = None
    if (agent_system or "").strip() or (agent_task or "").strip():
        from ksadk.prompts.resolved import (
            ResolvedPromptSources,
            compile_resolved_prompt_dict,
            get_default_platform_policy_source,
        )

        compiled_prompt = compile_resolved_prompt_dict(
            ResolvedPromptSources(
                agent_system=agent_system,
                agent_task=agent_task,
                request_instructions=normalized_instructions,
                platform_policy_source=get_default_platform_policy_source(),
            )
        )

    if resume_input is not None:
        if not session_id:
            raise ValueError("Responses resume input requires session_id")
        existing_events = await service.get_events(resolved_session_id)
        normalized_resume_input = dict(resume_input)
        if _is_checkpoint_resume_input(normalized_resume_input):
            normalized_resume_input = _normalize_checkpoint_resume_input(normalized_resume_input)
            await append_run_resume_event(
                session_id=resolved_session_id,
                author=agent_id,
                run_id=str(normalized_resume_input["run_id"]),
                checkpoint_id=str(normalized_resume_input["checkpoint_id"]),
                resume_attempt_id=str(normalized_resume_input["resume_attempt_id"]),
                framework=str(normalized_resume_input["framework"]),
                framework_ref=normalized_resume_input["framework_ref"],
                invocation_id=resolved_invocation_id,
                session_service_provider=provider,
            )
            # 补写 run_status(resuming)：让 ActiveRunStatus 在 resume 期间正确反映"恢复中"。
            # append_run_resume_event 写的是 run_resume 事件（status=resuming），而
            # _latest_session_run_status 只扫 run_status 事件 → 不补写则
            # resuming 不进 ActiveRunStatus。
            await append_run_status_event(
                session_id=resolved_session_id,
                author=agent_id,
                status="resuming",
                invocation_id=resolved_invocation_id,
                detail="checkpoint_resume",
                session_service_provider=provider,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )
            event_history = await service.get_events(resolved_session_id)
            history = build_history_from_events(event_history)
            responses_history = project_responses_history(event_history)
            return PreparedConversationTurn(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                user_input="",
                user_display_input="",
                history=history,
                responses_history=responses_history,
                input_content=[],
                input_messages=[],
                user_parts=[],
                attachments=[],
                attachment_results=[],
                current_attachments=[],
                current_attachment_results=[],
                has_current_files=False,
                model_metadata=resolved_model_metadata,
                model_options=normalized_model_options,
                instructions=normalized_instructions,
                request_metadata={
                    **normalized_request_metadata,
                    **_agentengine_resume_metadata(normalized_resume_input),
                },
                resume_input=normalized_resume_input,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
                shadow_context_plan=minimal_shadow_context_plan_dict(
                    runner=runner, runtime_type=runtime_type, deployment_mode=deployment_mode
                ),
                compiled_prompt=None,
                memory_write_rollout=memory_write_rollout,
                memory_enabled=memory_enabled,
                memory_recall_enabled=memory_recall_enabled,
                memory_write_mode=memory_write_mode,
                flush_before_compaction=flush_before_compaction,
                provider_ref=provider_ref,
            )

        is_approval_resume = _is_approval_resume_input(normalized_resume_input)
        existing_tool_receipt_event = None
        if is_approval_resume and not _has_pending_approval(existing_events):
            replay_candidate = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
                include_resolved=True,
            )
            receipt_key = _tool_receipt_idempotency_key_for_resume(
                session_id=resolved_session_id,
                resume_input=replay_candidate,
            )
            if receipt_key:
                existing_tool_receipt_event = _find_tool_receipt_event_by_key(
                    existing_events,
                    receipt_key,
                )
            if existing_tool_receipt_event is not None:
                normalized_resume_input = replay_candidate
            else:
                raise ValueError("Responses resume input requires a pending approval_request")
        if is_approval_resume:
            normalized_resume_input = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
            )
            caller_run_mode = _approval_resume_run_mode(
                existing_events,
                normalized_resume_input,
                fallback=caller_run_mode,
            )
            if governance_state is not None:
                governance_state.consecutive_approval_denials = (
                    _consecutive_approval_denials_from_events(existing_events)
                )

        resume_text = _format_resume_response_text(normalized_resume_input)
        resume_event_metadata: dict[str, Any] = {"resume_input": normalized_resume_input}
        if normalized_resume_input.get("call_id"):
            resume_event_metadata["tool_call_id"] = str(normalized_resume_input["call_id"])
        if "output" in normalized_resume_input:
            resume_event_metadata["tool_output"] = normalized_resume_input.get("output")
        await append_conversation_event(
            session_id=resolved_session_id,
            author="user",
            role="user",
            text=resume_text,
            invocation_id=resolved_invocation_id,
            event_type="approval_response" if is_approval_resume else "tool_result",
            session_service_provider=provider,
            metadata=resume_event_metadata,
        )
        if is_approval_resume and governance_state is not None:
            decision = normalized_resume_input.get("approval")
            if not isinstance(decision, Mapping):
                decision = _approval_decision_from_resume(normalized_resume_input)
            _governance_record_approval_response(governance_state, decision)
        tool_resume_input = None
        if is_approval_resume:
            tool_resume_input = await _execute_approved_builtin_tool_resume(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                resume_input=normalized_resume_input,
                session_service_provider=provider,
                existing_events=existing_events,
            )
        effective_resume_input = tool_resume_input or normalized_resume_input
        if tool_resume_input is not None:
            # ToolGateway approvals complete after an otherwise normal tool
            # call. Only LangGraph needs this private marker to restart a
            # terminal graph semantically; runtime_input strips it elsewhere.
            effective_resume_input = {
                **tool_resume_input,
                "_ksadk_gateway_approval_resume": True,
            }
        del existing_events
        event_history = await service.get_events(resolved_session_id)
        history = build_history_from_events(event_history)
        responses_history = project_responses_history(event_history)
        return PreparedConversationTurn(
            session_id=resolved_session_id,
            invocation_id=resolved_invocation_id,
            user_input=resume_text,
            user_display_input=resume_text,
            history=history,
            responses_history=responses_history,
            input_content=[],
            input_messages=[],
            user_parts=[],
            attachments=[],
            attachment_results=[],
            current_attachments=[],
            current_attachment_results=[],
            has_current_files=False,
            model_metadata=resolved_model_metadata,
            model_options=normalized_model_options,
            instructions=normalized_instructions,
            request_metadata=normalized_request_metadata,
            resume_input=effective_resume_input,
            run_mode=caller_run_mode,
            run_trigger=RUN_TRIGGER_APPROVAL_RESUME,
            shadow_context_plan=build_shadow_context_plan_dict(
                instructions=normalized_instructions,
                history=history,
                user_input=resume_text,
                request_metadata=normalized_request_metadata,
                runner=runner,
                runtime_type=runtime_type,
                model_metadata=resolved_model_metadata,
                deployment_mode=deployment_mode,
            ),
            compiled_prompt=None,
            memory_write_rollout=memory_write_rollout,
            memory_enabled=memory_enabled,
            memory_recall_enabled=memory_recall_enabled,
            memory_write_mode=memory_write_mode,
            flush_before_compaction=flush_before_compaction,
            provider_ref=provider_ref,
        )

    normalized_messages = _normalized_conversation_messages(messages)
    (
        user_input,
        user_display_input,
        input_content,
        user_parts,
        attachments,
        attachment_results,
    ) = _latest_user_turn(normalized_messages)
    input_messages = _canonical_input_messages(normalized_messages)
    effective_attachments, effective_attachment_results = _resolve_effective_attachment_context(
        normalized_messages=normalized_messages,
        session=session,
    )
    effective_state_delta = _build_attachment_context_state_delta(
        base_state_delta=state_delta,
        attachments=attachments,
        attachment_results=attachment_results,
    )
    event_metadata: dict[str, Any] = {
        "agent_input": user_input,
        "attachments": [compact_attachment_for_session(item) for item in attachments if item],
        "attachment_results": [
            compact_attachment_result_for_session(item) for item in attachment_results if item
        ],
    }

    if normalized_instructions:
        event_metadata["instructions"] = normalized_instructions
    deferred_tool_names = _latest_deferred_tool_names(await service.get_events(resolved_session_id))
    if deferred_tool_names and "deferred_tool_names" not in normalized_request_metadata:
        normalized_request_metadata["deferred_tool_names"] = deferred_tool_names
    if normalized_custom_metadata:
        event_metadata["request_metadata"] = normalized_custom_metadata
    if normalized_request_metadata:
        event_metadata["runtime_metadata"] = normalized_request_metadata

    await append_conversation_event(
        session_id=resolved_session_id,
        author="user",
        role="user",
        text=user_display_input or user_input,
        invocation_id=resolved_invocation_id,
        event_type="user_message",
        state_delta=effective_state_delta,
        content=_user_event_content(
            user_input=user_input,
            user_display_input=user_display_input,
            input_content=input_content,
            user_parts=user_parts,
        ),
        session_service_provider=provider,
        metadata=event_metadata,
    )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=user_input or user_display_input,
    )

    # compaction_owner 硬门控（方案 §6.2）：从 capability 取 owner，非 ksadk 时不走双阈值
    from ksadk.context_engine.capabilities import (
        capabilities_for_runner,
        capabilities_for_runtime_type,
    )

    _caps = (
        capabilities_for_runner(runner)
        if runner is not None
        else capabilities_for_runtime_type(runtime_type)
    )
    checkpoint = await _compact_conversation_history_with_governance(
        governance_state,
        session_id=resolved_session_id,
        author=agent_id,
        invocation_id=resolved_invocation_id,
        model=model,
        model_metadata=resolved_model_metadata,
        session_service_provider=provider,
        # PR D1：双阈值门控透传。ksadk_hosted → soft/hard proactive compact；否则旧单阈值。
        prompt_integration_mode=prompt_integration_mode,
        compaction_owner=_caps.compaction_owner,
    )
    event_history = await service.get_events(resolved_session_id)
    history = build_history_from_events(event_history)
    responses_history = project_responses_history(event_history)
    request_history = build_request_history(normalized_messages[:-1])
    # Gateway / Responses callers may send full prompt context while the
    # runtime-local session is empty or stale (for example after pod
    # replacement). Preserve that request context, but do not duplicate it when
    # local session events already contain the same prefix.
    history = _merge_request_history_with_session_history(request_history, history)
    request_responses_history = build_responses_history_from_messages(normalized_messages[:-1])
    responses_history = _merge_responses_history_with_session_history(
        request_responses_history,
        responses_history,
    )
    # The current user event is persisted before context construction so an
    # interrupted turn remains auditable.  That event belongs to
    # ``current_input`` though, not to prior history.  Keep the legacy
    # ``prepared.history`` contract unchanged for non-hosted paths, while the
    # KsADK-owned planner receives only events from earlier invocations.  Using
    # invocation_id (instead of text equality) also handles users deliberately
    # repeating the same message across turns.
    hosted_history = _merge_request_history_with_session_history(
        request_history,
        build_history_from_events(
            [event for event in event_history if event.invocation_id != resolved_invocation_id]
        ),
    )
    # PR D2：取最新 checkpoint 的 WorkingState（仅 ksadk_hosted 路径重注入）。
    # 非 ksadk_hosted → working_state=None（零注入，Runner 输入与旧逻辑一致）。
    # PTL retry 后 _refresh_history 也会重读 events，但此处 build_run_input 首次构建时取一次即可；
    # PTL 路径若产生新 checkpoint，retry 用 prepared 已有 working_state（保守：不中途换）。
    working_state: dict[str, Any] | None = None
    if prompt_integration_mode == "ksadk_hosted":
        working_state = _latest_checkpoint_working_state(event_history)

    prepared = PreparedConversationTurn(
        session_id=resolved_session_id,
        invocation_id=resolved_invocation_id,
        user_id=resolved_user_id,
        agent_id=agent_id,
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        history=history,
        request_history=request_history,
        request_responses_history=request_responses_history,
        responses_history=responses_history,
        input_content=input_content,
        input_messages=input_messages,
        user_parts=user_parts,
        attachments=effective_attachments,
        attachment_results=effective_attachment_results,
        current_attachments=attachments,
        current_attachment_results=attachment_results,
        has_current_files=bool(attachments or _parts_include_file(user_parts)),
        model_metadata=resolved_model_metadata,
        model_options=normalized_model_options,
        instructions=normalized_instructions,
        request_metadata=normalized_request_metadata,
        compaction_triggered=checkpoint is not None,
        compaction_trigger=(
            str((checkpoint.metadata or {}).get("trigger") or "auto") if checkpoint else None
        ),
        compacted_until_seq_id=(
            int((checkpoint.metadata or {}).get("compacted_until_seq_id") or 0)
            if checkpoint
            else None
        ),
        run_mode=caller_run_mode,
        run_trigger=caller_run_trigger,
        shadow_context_plan=build_shadow_context_plan_dict(
            instructions=normalized_instructions,
            history=hosted_history if prompt_integration_mode == "ksadk_hosted" else history,
            user_input=user_input,
            request_metadata=normalized_request_metadata,
            runner=runner,
            runtime_type=runtime_type,
            model_metadata=resolved_model_metadata,
            prompt_shadow=compiled_prompt,
            prompt_integration_mode=prompt_integration_mode,
            deployment_mode=deployment_mode,
        ),
        compiled_prompt=compiled_prompt,
        prompt_integration_mode=prompt_integration_mode,
        working_state=working_state,
        memory_write_rollout=memory_write_rollout,
        memory_enabled=memory_enabled,
        memory_recall_enabled=memory_recall_enabled,
        memory_write_mode=memory_write_mode,
        flush_before_compaction=flush_before_compaction,
        provider_ref=provider_ref,
    )
    # PR E：ksadk_hosted + V2 开关时运行真实 hosted 链路，回填 context_plan/assembled_input。
    # 失败回退空字段（prepared 字段语义完整），不阻断主链路。
    await _maybe_fill_hosted_pipeline(
        prepared,
        compiled_prompt=compiled_prompt,
        user_input=user_input,
        history=hosted_history,
        working_state=working_state,
        model_metadata=resolved_model_metadata,
        prompt_integration_mode=prompt_integration_mode,
        context_engine_rollout=context_engine_rollout,
        memory_recall_enabled=memory_recall_enabled,
        runtime_type=runtime_type,
        session_id=resolved_session_id,
        invocation_id=resolved_invocation_id,
        user_id=resolved_user_id,
        agent_id=agent_id,
        agent_max_input_tokens=agent_max_input_tokens,
        agent_reserve_output_tokens=agent_reserve_output_tokens,
    )
    return prepared


async def _maybe_fill_hosted_pipeline(
    prepared: PreparedConversationTurn,
    *,
    compiled_prompt: dict[str, Any] | None,
    user_input: str,
    history: list[dict[str, str]],
    working_state: dict[str, Any] | None,
    model_metadata: dict[str, Any],
    prompt_integration_mode: str,
    context_engine_rollout: str | None,
    memory_recall_enabled: bool | None,
    agent_max_input_tokens: int | None = None,
    agent_reserve_output_tokens: int | None = None,
    runtime_type: str | None,
    session_id: str,
    invocation_id: str,
    user_id: str,
    agent_id: str,
) -> None:
    """PR E：在 ksadk_hosted + V2 时运行 hosted 链路并回填 plan/assembly。

    门控三重：``KSADK_CONTEXT_ENGINE_V2_ENABLED`` × ``prompt_integration_mode=="ksadk_hosted"``
    × 已编译出含 prompt_content 的 CompiledPrompt（agent_system/agent_task 非空）。任一不满足
    → 不回填（走旧 PR B 分支，字节级一致）。

    仅对 ``prompt_owner=ksadk`` 的 runtime（langgraph 系）启用；native_runtime（codex）不进入，
    保证 Managed Codex 不被接管（方案 §6.2 / PCM-RUNNER-003）。
    """
    from ksadk.context_engine.capabilities import (
        assert_capability_not_circuit_open,
        capabilities_for_runtime_type,
    )
    from ksadk.context_engine.hosted_pipeline import (
        default_hosted_contributors,
        hosted_pipeline_enabled,
        run_hosted_pipeline,
    )

    if (
        not hosted_pipeline_enabled(rollout=context_engine_rollout)
        or prompt_integration_mode != "ksadk_hosted"
    ):
        return
    if (
        not isinstance(compiled_prompt, dict)
        or not str(compiled_prompt.get("prompt_content") or "").strip()
    ):
        return
    caps = capabilities_for_runtime_type(runtime_type)
    if caps.prompt_owner != "ksadk":
        return
    # 门禁：该 Runner 若已因 capability mismatch 熔断，回退旧路径（方案 §6.1）。不抛给主链路。
    try:
        assert_capability_not_circuit_open(runtime_type=runtime_type, label="hosted_pipeline")
    except Exception:  # noqa: BLE001
        logger.info("hosted pipeline skipped for session=%s: capability circuit open", session_id)
        return
    # PR E：注入默认 Contributors（MemoryRecall 等）进真实链路（方案 §8.7）。
    contributors = default_hosted_contributors(
        user_id=user_id,
        agent_id=agent_id,
        memory_recall_enabled=memory_recall_enabled,
    )
    try:
        result = await run_hosted_pipeline(
            compiled_prompt=compiled_prompt,
            user_input=user_input,
            history=history,
            working_state=working_state,
            model_metadata=model_metadata,
            contributors=contributors,
            # 与 shadow_plan 口径一致：ksadk_hosted + prompt_owner=ksadk + langgraph → ksadk_hosted
            integration_mode=(
                "ksadk_hosted"
                if prompt_integration_mode == "ksadk_hosted"
                and caps.prompt_owner == "ksadk"
                and runtime_type == "langgraph"
                else caps.integration_mode
            ),
            accounting_accuracy=caps.token_accounting,
            session_id=session_id,
            invocation_id=invocation_id,
            user_id=user_id,
            agent_id=agent_id,
            agent_max_input_tokens=agent_max_input_tokens,
            agent_reserve_output_tokens=agent_reserve_output_tokens,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "hosted pipeline failed for session=%s; falling back to PR B path",
            session_id,
        )
        return
    if result is None:
        return
    prepared.context_plan = result.plan
    prepared.assembled_input = {
        "format": result.assembled.format,
        "system": result.assembled.system,
        "messages": list(result.assembled.messages),
        "estimated_tokens": result.assembled.estimated_tokens,
        "warnings": list(result.assembled.warnings),
    }


def _latest_checkpoint_working_state(events: Sequence[SessionEvent]) -> dict[str, Any] | None:
    """取最新 context_checkpoint 事件的 working_state（PR D2）。

    checkpoint metadata 由 compact_conversation_history 写入（仅 ksadk_hosted）。
    无 checkpoint 或无 working_state 键时返回 None。
    """
    for event in reversed(list(events)):
        if canonical_event_type(event.event_type) != "context_checkpoint":
            continue
        meta = event.metadata or {}
        ws = meta.get("working_state")
        if isinstance(ws, dict):
            return ws
        return None
    return None


async def _refresh_history(
    prepared: PreparedConversationTurn, *, session_service_provider: Callable[[], Any] | None = None
) -> PreparedConversationTurn:
    """在 compaction 后刷新 prepared turn 的 history 视图。"""
    provider = session_service_provider or resolve_session_service
    service = provider()
    event_history = await service.get_events(prepared.session_id)
    prepared.history = _merge_request_history_with_session_history(
        prepared.request_history,
        build_history_from_events(event_history),
    )
    prepared.responses_history = _merge_responses_history_with_session_history(
        prepared.request_responses_history,
        project_responses_history(event_history),
    )
    return prepared
