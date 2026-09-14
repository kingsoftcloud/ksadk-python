"""Real child execution, durable approval bridging and cancellation lineage."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from ksadk.harness.engine import budgets
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.events import RuntimeEvent
from ksadk.harness.state import RunStatus
from ksadk.harness.subagent import (
    SubAgentExecutionError,
    SubAgentResult,
    child_spec,
    resequence_child_events,
    validate_subagent_output,
)
from ksadk.runtime import ResumePayload, ResumeTarget, RunHandle, StartRequest


class ChildApprovalPending(Exception):
    """Return a checkpointable child wait to the parent graph routing node."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__("child is awaiting an approval decision")


async def open_child(*, engine, parent_run, sub, task, call_id):
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
    from ksadk.harness.engine.thread_ids import encode_thread_id

    child_id = f"{parent_run.handle.run_id}:sub:{sub.name}:{call_id or 'delegation'}"
    active = engine._active_subagent_runs.setdefault(parent_run.handle.run_id, {})
    if call_id in active:
        child_engine, handle = active[call_id]
        return child_engine, handle, False
    child_agent = f"{parent_run.state.agent_id}:{sub.name}"
    checkpoint_session = f"{parent_run.state.session_id}:sub:{sub.name}:{call_id or 'delegation'}"
    spec = child_spec(parent_run.compiled.spec, sub)
    policy = parent_run.execution_policy
    child_context = (
        policy.child_system_context
        if policy is not None and policy.child_system_context is not None
        else policy.system_context
        if policy is not None
        else ""
    )
    if child_context:
        spec = spec.model_copy(
            update={
                "prompt": spec.prompt.model_copy(
                    update={"instructions": sub.instructions + "\n\n" + child_context}
                )
            }
        )
    # Parent controller ceilings also constrain children without a local config.
    config = dict(spec.execution_strategy.config)
    for kind, key in (
        ("tokens", "max_total_tokens"),
        ("tools", "max_tool_calls"),
        ("models", "max_model_calls"),
        ("artifacts", "max_artifacts"),
    ):
        inherited = budgets.limit(parent_run, kind)
        if inherited is not None:
            local = config.get(key)
            config[key] = inherited if local is None else min(inherited, int(local))
    spec = spec.model_copy(
        update={"execution_strategy": spec.execution_strategy.model_copy(update={"config": config})}
    )
    child_engine = ManagedLangGraphEngine(
        reasoner=engine._reasoner,
        checkpointer=engine._checkpointer,
        tenant_id=engine._tenant_id,
        tools={name: parent_run.tools[name] for name in sub.tools if name in parent_run.tools},
        approval_required=set(parent_run.approval_required) & set(sub.tools),
        capability_runtime=engine._capability_runtime,
        context_engine=engine._context_engine,
        memory_runtime=engine._memory_runtime,
        skill_runtime=engine._skill_runtime if sub.inherit_skills else None,
        mcp_runtime=engine._mcp_runtime if sub.inherit_mcp else None,
        artifact_store=getattr(engine._mcp_disclosure, "_artifact_store", None),
        mcp_offload_policy=engine._mcp_disclosure._offload_policy,
        max_reasoning_turns=min(sub.max_turns, engine._max_reasoning_turns),
    )
    compiled = await child_engine.compile(spec)
    request = StartRequest(
        input=task,
        user_id=parent_run.state.user_id,
        session_id=parent_run.state.session_id,
        agent_id=child_agent,
        runtime_type="managed-langgraph",
        metadata={
            # Preserve host context, but never inject the parent's conversation as
            # the child's task or reuse the parent's checkpoint namespace.
            **{
                key: value
                for key, value in parent_run.request.metadata.items()
                if key
                not in {"conversation_history", "invocation_id", "run_id", "checkpoint_session_id"}
            },
            "invocation_id": child_id,
            "parent_run_id": parent_run.handle.run_id,
            "checkpoint_session_id": checkpoint_session,
        },
    )
    candidate = RunHandle(
        run_id=child_id,
        session_id=parent_run.state.session_id,
        runtime_type="managed-langgraph",
        native_ref={
            "thread_id": encode_thread_id(
                tenant_id=engine._tenant_id,
                user_id=parent_run.state.user_id,
                agent_id=child_agent,
                session_id=checkpoint_session,
                run_id=child_id,
            ),
            "user_id": parent_run.state.user_id,
            "agent_id": child_agent,
            "parent_run_id": parent_run.handle.run_id,
        },
    )
    options = dict(
        execution_policy=parent_run.execution_policy,
        execution_policy_resolver=parent_run.execution_policy_resolver,
        execution_policy_request=parent_run.execution_policy_request,
    )
    handle, recovered = None, False
    if engine._checkpointer is not None:
        try:
            handle = await child_engine.attach(candidate, compiled, allow_completed=True, **options)
            recovered = True
        except ExecutionEngineError as exc:
            if "无未决 Checkpoint" not in str(exc):
                raise
    if handle is None:
        handle = await child_engine.start(request, compiled, **options)
    child_run = child_engine._runs[handle.run_id]
    budgets.adopt_child(parent_run, child_run)
    active[call_id] = (child_engine, handle)
    return child_engine, handle, recovered


async def execute_subagent(*, engine, parent_run, sub, task, call_id):
    child_engine, handle, recovered = await open_child(
        engine=engine, parent_run=parent_run, sub=sub, task=task, call_id=call_id
    )
    child_run = child_engine._runs[handle.run_id]
    collected: list[RuntimeEvent] = []
    final_text = next(
        (m.content for m in reversed(child_run.state.messages) if m.role.value == "assistant"), ""
    )
    failure, category = "", ""

    def forward(event):
        nonlocal final_text, failure, category
        collected.append(event)
        resequence_child_events(parent_run, [event], observe=True)
        if event.event_type == "text.completed" and event.phase == "final_answer":
            final_text = str(event.payload.get("text") or "")
        if event.event_type == "run.failed":
            failure = str(event.payload.get("error") or "child run failed")
            category = str(event.payload.get("error_category") or "child_failed")

    try:
        while not child_run.done:
            if child_run.state.status is RunStatus.AWAITING_APPROVAL:
                if engine._checkpointer is None:
                    raise SubAgentExecutionError(
                        "approval_unavailable", "child approval requires checkpointing"
                    )
                info = dict(child_run.pending_approval)
                # The parent interrupt persists the exact child Run/Call. Its
                # decision is consumed only by that child's pending native call.
                detail = {
                    "call_id": call_id,
                    "name": sub.name,
                    "risk": "high",
                    "kind": "child_tool",
                    "child_run_id": handle.run_id,
                    "parent_run_id": parent_run.handle.run_id,
                    "child_call_id": child_run.pending_approval_call_id,
                    "child_handle": handle.model_dump(mode="json"),
                    "child_approval": info,
                }
                resolved = parent_run.child_approval_decision
                if not resolved:
                    raise ChildApprovalPending(detail)
                if (
                    resolved.get("child_run_id") != handle.run_id
                    or resolved.get("child_call_id") != child_run.pending_approval_call_id
                ):
                    raise ExecutionEngineError(
                        "child approval decision does not match pending execution"
                    )
                decision = resolved["decision"]
                parent_run.child_approval_decision = None
                await child_engine.resume(
                    handle,
                    ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
                    ResumePayload(
                        kind="approval_decision",
                        call_id=child_run.pending_approval_call_id,
                        data=decision,
                    ),
                )
            elif child_run.state.status is RunStatus.PAUSED:
                await child_engine.resume(
                    handle, ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]), None
                )
            # Approval wait time is excluded. Each executing segment remains
            # bounded and child limits remain cumulative across checkpoints.
            remaining_seconds = sub.timeout_seconds - child_run.execution_elapsed_seconds
            if remaining_seconds <= 0:
                raise TimeoutError

            async def consume_segment():
                async for event in child_engine.stream(handle):
                    forward(event)

            await asyncio.wait_for(consume_segment(), timeout=remaining_seconds)
            if child_run.state.status is RunStatus.AWAITING_APPROVAL:
                continue
            if not child_run.done:
                raise SubAgentExecutionError(
                    "child_interrupted", "child stopped without a terminal state"
                )
    except (TimeoutError, asyncio.TimeoutError):
        failure, category = f"sub-agent {sub.name!r} timed out", "timeout"
        await cancel_and_drain(child_engine, handle, forward)
    except asyncio.CancelledError:
        # Cancellation must await the child's native terminal event before the
        # parent publishes its terminal state; no detached child continues.
        await cancel_and_drain(child_engine, handle, forward)
        raise
    finally:
        if child_run.done:
            active = engine._active_subagent_runs.get(parent_run.handle.run_id, {})
            active.pop(call_id, None)
            if not active:
                engine._active_subagent_runs.pop(parent_run.handle.run_id, None)
    if child_run.state.status is RunStatus.CANCELED and not failure:
        failure, category = "child run canceled", "canceled"
    if child_run.state.status is RunStatus.FAILED and not failure:
        failure, category = "child run failed", "child_failed"
    if (
        failure
        and sub.failure_policy == "retry"
        and sub.max_retries > 0
        and category in {"child_failed", "timeout"}
    ):
        result, events = await execute_subagent(
            engine=engine,
            parent_run=parent_run,
            sub=replace(sub, max_retries=sub.max_retries - 1),
            task=task,
            call_id=f"{call_id}:retry-{sub.max_retries}",
        )
        return result, [*collected, *events]
    if failure and sub.failure_policy != "return_error":
        raise SubAgentExecutionError(category, failure)
    output = None
    if not failure:
        try:
            output = validate_subagent_output(
                final_text or "（子 Agent 未产出结果）", sub.output_schema
            )
        except ValueError as exc:
            raise SubAgentExecutionError("output_validation_failed", str(exc)) from exc
    return SubAgentResult(
        status="failed" if failure else "completed",
        output=output,
        artifact_refs=tuple(child_run.artifact_refs),
        usage={
            "total_tokens": child_run.budget_usage.get("tokens", 0),
            "input_tokens": child_run.budget_usage.get("input_tokens", 0),
            "output_tokens": child_run.budget_usage.get("output_tokens", 0),
        },
        evidence={
            "child_agent_id": child_run.state.agent_id,
            "child_run_id": handle.run_id,
            "parent_run_id": parent_run.handle.run_id,
            "child_handle": handle.model_dump(mode="json"),
            "recovered_from_checkpoint": recovered,
        },
        error_category=category or None,
        error=failure or None,
    ).model_dump(mode="json"), collected


async def cancel_and_drain(child_engine, handle, forward):
    await child_engine.cancel(handle)
    run = child_engine._runs[handle.run_id]
    if run.task is not None and not run.task.done():
        await asyncio.gather(run.task, return_exceptions=True)
    async for event in child_engine.stream(handle):
        forward(event)


async def restore_pending_child(engine, run):
    info = run.pending_approval
    if info.get("kind") != "child_tool":
        return
    sub = run.sub_agents.get(str(info.get("name") or ""))
    if sub is None:
        raise ExecutionEngineError("pending child definition is unavailable")
    await open_child(engine=engine, parent_run=run, sub=sub, task="", call_id=str(info["call_id"]))
