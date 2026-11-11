"""Typed effect dispatch. Ports receive detached requests, never a writable Store."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .artifact_runtime import govern_tool_result
from .child_lifecycle import (
    ChildLifecyclePort,
    ChildLifecycleRequest,
    lifecycle_request,
    validate_child_lifecycle_result,
)
from .completion import CompletionCandidate
from .context_contributions import ContextPort, collection_request, validate_collection
from .context_contributor_contract import ContextModelDispatchDenial, contributor_expired
from .hook_runtime_contract import (
    HookInvocationRequest,
    HookPort,
    _json_bytes,
    validate_hook_result,
)
from .model import ModelToolCall
from .model_request import model_request
from .model_runtime import exchange
from .ports import (
    ArtifactPort,
    ArtifactRef,
    ModelPort,
    RunConfiguration,
    ToolBudget,
    ToolPort,
    ToolRequest,
    ToolResult,
    VerificationBundle,
    VerificationPort,
    VerificationRequest,
    WorkspaceObservation,
    WorkspacePort,
    WorkspaceRequest,
)
from .precompact_hook_runtime import precompact_hook_request
from .request_media import ModelRequestDispatchDenial, RequestMediaError
from .state import EffectIntent, RunSnapshot
from .stop_hook_runtime import stop_hook_request
from .tool_budget import (
    dispatch_tool_budget,
)
from .tool_hook_runtime import tool_hook_denial, tool_hook_request
from .tool_input_validation import ToolInputValidator
from .tool_recovery import query_sealed_tool_result
from .verification_binding import verification_subject_context_digest


async def dispatch_effect(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    config: RunConfiguration,
    *,
    model: ModelPort,
    tools: ToolPort | None,
    verifier: VerificationPort | None,
    workspace: WorkspacePort | None,
    artifacts: ArtifactPort | None,
    emit,
    flush_callback: Callable[[], None] | None = None,
    flush_interval: float = 0.2,
    tool_budget: ToolBudget | None = None,
    on_parallel_child: Callable[[int, dict, str, ToolResult], Awaitable[None]] | None = None,
    reconcile_tool: bool = False,
    context_port: ContextPort | None = None,
    hook_port: HookPort | None = None,
    child_lifecycle_port: ChildLifecyclePort | None = None,
):
    key = intent.operation_key
    if intent.kind == "child":
        if config.child_lifecycle_policy is None or child_lifecycle_port is None:
            raise ValueError("Child lifecycle Port unavailable")
        request = ChildLifecycleRequest.model_validate(intent.payload["request"])
        if request != lifecycle_request(snapshot, key, request.action):
            raise ValueError("Child lifecycle intent differs from current allocations")
        result = await child_lifecycle_port.collect(request.model_copy(deep=True))
        return validate_child_lifecycle_result(request, result)
    if intent.kind == "hook":
        if hook_port is None:
            raise ValueError("Hook Port unavailable")
        request = HookInvocationRequest.model_validate(intent.payload["request"])
        expected = (stop_hook_request(snapshot, config, key) if request.event == "Stop"
                    else precompact_hook_request(snapshot, config, key)
                    if request.event == "PreCompact"
                    else tool_hook_request(snapshot, config, key))
        if expected is None or _json_bytes(request.model_dump(mode="json")) != \
                _json_bytes(expected.model_dump(mode="json")):
            raise ValueError("Hook intent differs from current admission")
        result = await hook_port.invoke(request.model_copy(deep=True))
        return validate_hook_result(request, result)
    if intent.kind == "context":
        if context_port is None:
            raise ValueError("Context Port unavailable")
        request = collection_request(snapshot, config, key, intent.payload.get("input_budget"))
        result = await context_port.collect(request.model_copy(deep=True))
        return validate_collection(request, result)
    if intent.kind == "workspace":
        if workspace is None:
            return WorkspaceObservation(
                operation_key=key,
                status="unavailable",
                reason="workspace_port_unavailable",
            )
        request = WorkspaceRequest(scope=snapshot.scope, run_id=snapshot.run_id, operation_key=key)
        result = await workspace.capture(request.model_copy(deep=True))
        return WorkspaceObservation.model_validate_json(result.model_dump_json())
    if intent.kind == "model":
        summary = intent.payload.get("purpose") == "compaction"
        if not summary and contributor_expired(snapshot.context_source_receipt):
            return ContextModelDispatchDenial(operation_key=key)
        review = intent.payload.get("purpose") == "delivery_review"
        try:
            request = model_request(snapshot, config, key=key, payload=intent.payload)
        except RequestMediaError:
            return ModelRequestDispatchDenial(operation_key=key)
        return await exchange(
            model, request,
            None if summary or review else emit,
            flush_callback=flush_callback,
            flush_interval=flush_interval,
        )
    if intent.kind == "tool":
        if intent.payload.get("parallel_readonly") is True:
            calls = intent.payload.get("calls")
            if not isinstance(calls, list) or len(calls) < 2:
                raise ValueError("invalid parallel tool effect")
            delegation = intent.payload.get("parallel_delegation") is True
            async def execute_one(index: int, raw_call: dict) -> ToolResult:
                call = ModelToolCall.model_validate(raw_call)
                definition = next((tool for tool in config.tools if tool.name == call.name), None)
                if definition is None or not definition.concurrent_safe or not definition.read_only:
                    raise ValueError("parallel tool lacks read-only host authority")
                if definition.delegated_read_only != delegation:
                    raise ValueError("parallel delegation grant mismatch")
                child = intent.model_copy(
                    update={
                        "operation_key": f"{key}:call:{index + 1}",
                        "payload": {"call": raw_call},
                    }
                )
                reserved = snapshot.model_copy(
                    update={"tool_calls": snapshot.tool_calls + len(calls) - 1}
                )
                result = await dispatch_effect(
                    reserved,
                    child,
                    config,
                    model=model,
                    tools=tools,
                    verifier=verifier,
                    workspace=workspace,
                    artifacts=artifacts,
                    emit=emit,
                    flush_callback=flush_callback,
                    flush_interval=flush_interval,
                    tool_budget=(
                        dispatch_tool_budget(snapshot, config, batch_size=len(calls), index=index)
                        if delegation
                        else None
                    ),
                )
                sealed = ToolResult.model_validate(result)
                if on_parallel_child is not None:
                    await on_parallel_child(index, raw_call, child.operation_key, sealed)
                return sealed

            tasks = [
                asyncio.create_task(execute_one(index, raw))
                for index, raw in enumerate(calls)
            ]
            try:
                results = await asyncio.gather(*tasks)
            finally:
                # gather does not stop siblings when one member fails or
                # cancels itself. The batch owns every member until cleanup;
                # the outer dispatcher still bounds an uncooperative port.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return ToolResult(
                status="error" if any(item.status != "ok" for item in results) else "ok",
                content={"batch_results": [item.model_dump(mode="json") for item in results]},
            )
        call = ModelToolCall.model_validate(intent.payload["call"])
        if call.wire_arguments_invalid:
            # A completed model tool call earns a matching error receipt, but
            # malformed provider bytes grant no authority to invoke a host.
            return ToolResult(
                status="uncertain" if reconcile_tool else "error",
                content={
                    "code": "malformed_tool_arguments",
                    "instruction": (
                        "Send a new call with a complete JSON object matching the tool schema."
                    ),
                },
            )
        definition = next((tool for tool in config.tools if tool.name == call.name), None)
        allowed = config.contract.allowed_tool_names
        if (
            definition is None
            or tools is None
            or (allowed is not None and call.name not in allowed)
        ):
            return ToolResult(
                status="uncertain" if reconcile_tool else "error",
                content={"reason": "tool_unavailable"},
            )
        if not reconcile_tool:
            if config.hooks and (denial := tool_hook_denial(snapshot, call)):
                return denial
            # A host may supply any ToolPort, not only StrictToolExecutor.
            # Reject schema failures before that Port can run policy, Hooks
            # or effects. Reconciliation queries an already-issued operation;
            # it must not be replaced by a new pre-execution rejection.
            try:
                validator = ToolInputValidator(definition.parameters)
            except ValueError:
                return ToolResult(status="error", content={"code": "invalid_tool_schema"})
            if rejection := validator.rejection(call.arguments):
                return ToolResult(status="error", content=rejection)
        request = ToolRequest(
            scope=snapshot.scope,
            run_id=snapshot.run_id,
            operation_key=key,
            call=call,
            definition=definition,
            interaction=snapshot.interaction,
            child_approval=snapshot.child_approval,
            budget=tool_budget
            or dispatch_tool_budget(snapshot, config),
            result_delivery="runtime_managed",
            available_artifacts=tuple(
                ArtifactRef.model_validate(ref) for ref in snapshot.artifact_refs
            ),
        )
        if reconcile_tool:
            result = await query_sealed_tool_result(
                tools, request, method=(
                    "resume_checkpoint"
                    if config.tool_recovery_policy == "resume_local_child_checkpoint/v1"
                    else "resume_prepared"
                    if config.tool_recovery_policy == "resume_prepared_local_child/v1"
                    else "recover_prepared"
                    if config.tool_recovery_policy == "prepared_local_child/v1"
                    else "reconcile"
                ),
            )
        else:
            from ksadk.harness.tool_output import (
                PublicOutputBuffer,
                ToolOutputObservation,
                tool_output_scope,
            )

            output = PublicOutputBuffer(call.name, call.arguments, lambda display: emit(
                ToolOutputObservation("tool_output", key, call.call_id, display)
            ))
            with tool_output_scope(output.observe):
                try:
                    result = await tools.execute(request.model_copy(deep=True))
                finally:
                    output.finish()
        sealed = ToolResult.model_validate_json(result.model_dump_json())
        from .child_budget import validate_child_budget_admission

        validate_child_budget_admission(request, sealed)
        return await govern_tool_result(
            request=request,
            result=sealed,
            artifacts=artifacts,
            max_result_bytes=config.limits.max_tool_result_bytes,
            max_image_bytes=config.limits.max_tool_image_bytes,
        )
    subject_context_digest = verification_subject_context_digest(snapshot)
    request = VerificationRequest(
        scope=snapshot.scope,
        run_id=snapshot.run_id,
        candidate_id=intent.payload["candidate_id"],
        final_text=intent.payload["final_text"],
        purpose=intent.payload.get("purpose", "final"),
        contract=config.contract,
        subject_context_digest=subject_context_digest,
    )
    if verifier is None:
        return VerificationBundle(
            candidate=CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest="unavailable",
                subject_context_digest=subject_context_digest,
                environment_digest="unavailable",
            ),
            report=None,
        )
    result = await verifier.verify(request.model_copy(deep=True))
    bundle = VerificationBundle.model_validate_json(result.model_dump_json())
    if workspace is None:
        return bundle
    post_key = f"{key}:post-verification"
    observed = await workspace.capture(
        WorkspaceRequest(scope=snapshot.scope, run_id=snapshot.run_id, operation_key=post_key)
    )
    post = WorkspaceObservation.model_validate_json(observed.model_dump_json())
    return bundle.model_copy(update={"post_workspace": post})
