"""Commit Native effect receipts, including parallel members and stream observations."""

from __future__ import annotations

import asyncio
from time import time

from .call_budget import call_room
from .child_interruption import admit_interrupted_child_receipts
from .child_lifecycle import (
    ChildLifecycleRequest,
    ChildLifecycleResult,
    recover_paused_child_observation,
    reduce_child_lifecycle,
)
from .child_lifecycle_contract import ChildInterruptedReceiptRequest
from .context_contributions import reduce_collection
from .context_contributor_contract import ContextModelDispatchDenial
from .context_reduction import SUMMARY_REDUCTION_POLICY
from .finalization_workspace import reduce_finalization_workspace
from .hook_runtime_contract import HookInvocationResult
from .machine import (
    reduce_model,
    reduce_parallel_readonly_tools,
    reduce_tool,
    reduce_workspace,
)
from .model import ModelStreamEvent, ModelTurnResult
from .model_runtime import (
    StreamFlushError,
    provisional_delta,
    recover_unsealed_model_receipt,
    retry_delay,
    sealed_model_usage_event,
    unavailable_uncertain_usage,
)
from .ports import (
    ToolResult,
    VerificationBundle,
    WorkspaceObservation,
)
from .precompact_hook_runtime import reduce_precompact_hook
from .request_media import ModelRequestDispatchDenial
from .state import (
    EffectIntent,
    EffectReceipt,
    RunPhase,
    RunSnapshot,
)
from .stop_hook_runtime import reduce_stop_hook
from .stream_display_buffer import _DISPLAY, StreamDisplayBuffer
from .tool_hook_runtime import (
    close_tool_hook_run,
    consume_hook_feedback,
    reduce_tool_hook,
    stage_post_hooks,
)
from .transitions import commit_transition, finish
from .verification_runtime import reduce_verification


class EffectReceiptCommitter:
    def _interrupted_child_receipt(
        self, snapshot: RunSnapshot, intent: EffectIntent, receipt: EffectReceipt,
    ) -> EffectReceipt:
        if (self.configuration.child_lifecycle_policy != "owned-background/v2"
                or intent.kind != "tool" or receipt.status == "committed"):
            return receipt
        frames = self._child_promotion.interrupted_receipts(ChildInterruptedReceiptRequest(
            scope=self.scope, run_id=snapshot.run_id, parent_operation_key=intent.operation_key,
        ))
        admitted = admit_interrupted_child_receipts(
            snapshot, intent, self.effective_configuration(snapshot), frames,
        )
        if not admitted:
            return receipt
        if intent.payload.get("parallel_delegation") is True:
            for index, call, frame in admitted:
                self._commit_parallel_member(snapshot.run_id, intent, index, call,
                                             frame.parent_operation_key, frame.result)
            return receipt
        return EffectReceipt(operation_key=intent.operation_key, status="committed",
                             result=admitted[0][2].result.model_dump(mode="json"))

    async def _commit_parallel_child(
        self,
        run_id: str,
        parent_intent: EffectIntent,
        index: int,
        raw_call: dict,
        operation_key: str,
        result: ToolResult,
    ) -> None:
        """Seal a member's observation and accounting before the batch ends.

        The parent drive lock is held by ``advance``; child callbacks use a
        separate lock so completions cannot race one another's revision. A
        canceled sibling cannot erase a child that already completed.
        """
        async with self._parallel_progress_locks.setdefault(run_id, asyncio.Lock()):
            self._commit_parallel_member(
                run_id, parent_intent, index, raw_call, operation_key, result,
            )

    def _commit_parallel_member(
        self, run_id: str, parent_intent: EffectIntent, index: int, raw_call: dict,
        operation_key: str, result: ToolResult,
    ) -> None:
        # The caller owns the parent driver lock. The same original receipt
        # reducer serves normal callbacks and synchronous interruption harvest.
        snapshot = self.snapshot(run_id)
        calls = parent_intent.payload.get("calls")
        if (
            snapshot.pending_intent != parent_intent.operation_key
            or snapshot.phase.terminal
            or not isinstance(calls, list)
            or index >= len(calls)
            or calls[index] != raw_call
            or operation_key != f"{parent_intent.operation_key}:call:{index + 1}"
        ):
            raise ValueError("parallel child no longer belongs to the active batch")
        call_id = raw_call["call_id"]
        member_intent = self.store.get_intent(self.scope, run_id, operation_key)
        if member_intent is None and parent_intent.payload.get("parallel_delegation") is not True:
            return  # Legacy read batch, without durable member admission.
        if (member_intent is None or member_intent.kind != "tool"
                or member_intent.status != "running"
                or member_intent.payload != {"call": raw_call}):
            raise ValueError("parallel member admission mismatch")
        sealed = EffectReceipt(operation_key=operation_key, status="committed",
                               result=result.model_dump(mode="json"))
        existing = self.store.get_receipt(self.scope, run_id, operation_key)
        if existing is not None:
            if existing != sealed:
                raise ValueError("parallel member receipt changed")
            return
        from .child_budget import apply_child_budget

        definition = next(
            (tool for tool in self.effective_configuration(snapshot).tools
             if tool.name == raw_call["name"]), None
        )
        delegated = parent_intent.payload.get("parallel_delegation") is True
        from .tool_observation import (
            append_executed_tool_message,
            executed_tool_message,
            ordered_tool_artifact_refs,
            parallel_result_violation,
        )

        valid = not parallel_result_violation(result, delegated=delegated)
        allocated, usage = apply_child_budget(
            snapshot, result if delegated or valid else ToolResult(status="error"), definition,
            parent_operation_key=operation_key, parent_tool_call_id=call_id,
        )
        updated = allocated.model_copy(
            update={
                "descendant_model_calls": snapshot.descendant_model_calls
                + usage.model_calls,
                "descendant_tool_calls": snapshot.descendant_tool_calls + usage.tool_calls,
                "child_run_ids": tuple(
                    dict.fromkeys((*allocated.child_run_ids, *usage.child_run_ids))
                ),
                "parallel_child_settlements": (
                    *snapshot.parallel_child_settlements,
                    call_id,
                ) if delegated else snapshot.parallel_child_settlements,
            }
        )
        observed = False
        if valid:
            from .ports import ArtifactRef
            from .tool_hook_runtime import hook_execution_metadata

            artifacts = {ref.uri: ref for ref in (
                ArtifactRef.model_validate(item) for item in snapshot.artifact_refs
            )}
            if not any(ref.uri in artifacts and artifacts[ref.uri] != ref
                       for ref in result.artifact_refs):
                artifacts.update((ref.uri, ref) for ref in result.artifact_refs)
                message = executed_tool_message(
                    result, call_id=call_id, definition=definition,
                    receipt_ref=parent_intent.operation_key, goal_revision=snapshot.goal_revision,
                    tool_call=snapshot.tool_calls + index + 1,
                    hook_metadata=hook_execution_metadata(snapshot.model_copy(update={
                        "pending_tools": (raw_call,),
                    })),
                )
                messages = append_executed_tool_message(snapshot.messages, message)
                updated = updated.model_copy(update={
                    "messages": messages,
                    "artifact_refs": ordered_tool_artifact_refs(
                        tuple(ref.model_dump(mode="json") for ref in artifacts.values()),
                        messages, parent_intent.operation_key,
                        parent_intent.payload.get("parallel_artifact_base"),
                    ),
                })
                observed = True
        usage_event = ()
        if usage.child_run_ids or usage.model_calls:
            known = all(
                value is not None
                for value in (
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_input_tokens,
                    usage.reasoning_tokens,
                )
            )
            usage_event = (
                (
                    "usage.reported" if known else "usage.unavailable",
                    {
                        "semantics": "delta",
                        "request_id": operation_key,
                        "attempt_id": operation_key + ":child-accounting",
                        "source": "descendant_runs",
                        "child_run_ids": list(usage.child_run_ids),
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cached_input_tokens": usage.cached_input_tokens,
                        "reasoning_tokens": usage.reasoning_tokens,
                    },
                ),
            )
        self._commit(
            snapshot,
            commit_transition(
                snapshot,
                updated,
                (("tool.child_reserved" if result.child_budget_reservation
                  else "tool.child_settled") if delegated else "tool.member_settled"),
                {
                    "call_id": call_id,
                    "child_run_ids": list(usage.child_run_ids),
                    "status": result.status,
                    "parent_operation_key": parent_intent.operation_key,
                    "member_receipt_ref": operation_key,
                    "model_observation_committed": observed,
                    **({"tool_display": result.public_display} if result.public_display else {}),
                },
                receipts=(sealed,),
                extra_events=usage_event,
            ),
        )

    def _flush_stream_guarded(self, run_id: str) -> None:
        """Flush pending display via _flush_stream, wrapping store errors
        as StreamFlushError.

        All display commits from _stream_delta (immediate first display,
        key-change old pending, size threshold, coalesced pending) route
        through this method so that StoreConflict/CAS/fencing errors are
        distinguishable from protocol errors. Without this wrapper, a
        bare StoreConflict from _flush_stream would propagate through
        emit → exchange → effect_control and be masked as a generic
        uncertain receipt (effect_outcome_unknown), hiding a local store
        fault from the host.
        """
        try:
            self._flush_stream(run_id)
        except Exception as exc:
            raise StreamFlushError(str(exc)) from exc

    def _stream_delta(self, run_id, event):
        # The owning drive lock remains held during this callback.
        current = self.snapshot(run_id)
        from ksadk.harness.tool_output import ToolOutputObservation

        if isinstance(event, ToolOutputObservation):
            parent = current.pending_intent
            if (parent is None or not (
                event.operation_key == parent or event.operation_key.startswith(f"{parent}:call:")
            ) or not any(call["call_id"] == event.call_id for call in current.pending_tools)):
                raise ValueError("tool output no longer belongs to the active request")
            self._commit(current, commit_transition(current, current, "tool.output.updated", {
                "operation_key": event.operation_key, "call_id": event.call_id,
                "tool_display": event.display,
            }))
            return
        if event.kind == "input_metadata":
            from .model_input import input_binding_failure

            if input_binding_failure(self.effective_configuration(current).profile,
                                     event.input_receipt):
                raise ValueError("model input observation profile mismatch")
            self._commit(current, provisional_delta(current, event))
            return
        # Display events (text_delta, public_summary): ALL display commits
        # route through _flush_stream_guarded for prepared-batch retention
        # and StreamFlushError boundary. This ensures StoreConflict/CAS/
        # fencing errors are distinguishable from protocol errors and are
        # not masked as generic uncertain receipts by effect_control.
        if isinstance(event, ModelStreamEvent) and event.kind in _DISPLAY:
            buffer = self._stream_buffers.setdefault(run_id, StreamDisplayBuffer())
            pending = buffer.peek()
            if pending is not None and pending.request_id != current.pending_intent:
                buffer = StreamDisplayBuffer()
                self._stream_buffers[run_id] = buffer
            immediate, flush_now = buffer.push(event)
            for ev in immediate:
                if ev is buffer.peek():
                    # Old pending from key change — already in buffer.
                    self._flush_stream_guarded(run_id)
                else:
                    # First display for key, or new key's first event.
                    buffer.set_pending(ev)
                    self._flush_stream_guarded(run_id)
            if flush_now:
                # Size threshold: pending is already in buffer.
                self._flush_stream_guarded(run_id)
            return
        self._commit(current, provisional_delta(current, event))

    def _commit_receipt(
        self, snapshot: RunSnapshot, intent: EffectIntent, receipt: EffectReceipt,
        *, reconciled: bool = False, interruption: EffectReceipt | None = None,
    ) -> RunSnapshot:
        extra = ()
        sealed_model_response = None
        config = self.effective_configuration(snapshot)
        model_recovery = recover_unsealed_model_receipt(
            snapshot, intent, receipt, config, now=time()
        )
        child_observation = recover_paused_child_observation(snapshot, intent, receipt, config)
        if model_recovery is not None:
            updated, usage_event = model_recovery
            extra = (usage_event,)
        elif child_observation is not None:
            updated = child_observation
            extra = (("child.observation_paused", {
                "operation_key": intent.operation_key,
                "action": intent.payload["request"]["action"],
            }),)
        elif intent.kind == "workspace" and intent.payload.get("purpose") == "finalization":
            updated = reduce_finalization_workspace(snapshot, intent, receipt, config)
            if updated.reason == "wall_time_hard_limit":
                extra = (("budget.exhausted", {"reason": "wall_time_hard_limit"}),)
        elif receipt.status != "committed":
            wall_expired = receipt.result.get("reason") == "wall_time_hard_limit"
            incomplete = snapshot.model_copy(update={"candidate": None}) if (
                intent.kind == "hook"
            ) else snapshot
            if intent.kind == "child":
                incomplete = incomplete.model_copy(update={
                    "candidate": None,
                    "child_lifecycle_failure": intent.payload["request"]["action"],
                })
            updated = finish(
                incomplete, "budget_exhausted" if wall_expired else "unverified",
                "wall_time_hard_limit" if wall_expired else "effect_outcome_unknown",
            )
            if intent.kind == "hook" and snapshot.phase == RunPhase.EXECUTING_TOOLS:
                updated = close_tool_hook_run(incomplete, updated.outcome, updated.reason)
            if wall_expired or receipt.result.get("deadline_cause") == "wall_time_hard_limit":
                extra = (("budget.exhausted", {"reason": "wall_time_hard_limit"}),)
            counter = (
                "model_calls"
                if intent.kind == "model"
                else "tool_calls"
                if intent.kind == "tool"
                else None
            )
            if counter:
                updated = updated.model_copy(update={
                    counter: getattr(snapshot, counter) + intent.loop_attempts,
                })
            if intent.kind == "model" and receipt.status == "uncertain":
                extra = (*extra, unavailable_uncertain_usage(intent.operation_key))
        elif (intent.kind == "model" and receipt.result.get("schema_version")
              == "ksadk.native-context-model-denial/v1"):
            denial = ContextModelDispatchDenial.model_validate(receipt.result)
            if denial.operation_key != intent.operation_key:
                raise ValueError("Context model denial identity mismatch")
            # The admitted attempt consumes its loop budget, but the host
            # knows no ModelPort ran. Do not fabricate a provider response,
            # zero usage, or a provider-usage-missing event.
            updated = finish(snapshot.model_copy(update={
                "model_calls": snapshot.model_calls + 1,
            }), "blocked", denial.reason)
            extra = (("model.dispatch_denied", denial.model_dump(mode="json")),)
        elif (intent.kind == "model" and receipt.result.get("schema_version")
              == "ksadk.native-model-request-denial/v1"):
            denial = ModelRequestDispatchDenial.model_validate(receipt.result)
            if denial.operation_key != intent.operation_key:
                raise ValueError("Model request denial identity mismatch")
            updated = finish(snapshot.model_copy(update={
                "model_calls": snapshot.model_calls + 1,
            }), "blocked", denial.reason)
            extra = (("model.dispatch_denied", denial.model_dump(mode="json")),)
        elif intent.kind == "hook":
            result = HookInvocationResult.model_validate(receipt.result)
            updated = (reduce_stop_hook(snapshot, config, intent.operation_key, receipt.result)
                       if result.event == "Stop" else
                       reduce_precompact_hook(
                           snapshot, config, intent.operation_key, receipt.result)
                       if result.event == "PreCompact" else
                       reduce_tool_hook(snapshot, config, intent.operation_key, receipt.result))
            extra = (("hook.received", {
                "event": result.event, "hook_id": result.hook_id,
                "status": result.status, "reason": result.reason,
            }),)
        elif intent.kind == "context":
            updated = reduce_collection(snapshot, config, intent.operation_key, receipt.result,
                                        intent.payload.get("input_budget"),
                                        count_message=self.context.planner._count_message)
            extra = (("context.sources_collected", {
                "goal_revision": snapshot.goal_revision,
                "source_ids": [item.source_id for item in config.context_sources],
                "admitted": (
                    updated.context_source_goal_revision == snapshot.goal_revision
                    and (updated.context_source_receipt or {}).get("operation_key")
                    == intent.operation_key
                ),
            }),)
        elif intent.kind == "child":
            request = ChildLifecycleRequest.model_validate(intent.payload["request"])
            updated, usage_events = reduce_child_lifecycle(
                snapshot, request, ChildLifecycleResult.model_validate(receipt.result),
                config,
            )
            extra = (*usage_events, ("child.lifecycle_received", {
                "action": request.action,
                "child_run_ids": [item.child_run_id for item in request.reservations],
            }))
            if updated.child_approval is not None:
                from .child_approval import approval_event
                extra = (*extra, approval_event(updated))
        elif intent.kind == "workspace":
            updated = reduce_workspace(
                snapshot,
                WorkspaceObservation.model_validate(receipt.result),
                str(intent.payload.get("cursor", "")),
                config,
            )
        elif intent.kind == "model":
            result = ModelTurnResult.model_validate(receipt.result)
            sealed_model_response = result
            if intent.payload.get("purpose") == "compaction":
                updated = self.context.seal(snapshot, result, config)
                bounded_summary = (
                    snapshot.compaction_request.get("summary_completion_policy")
                    == "bounded-committed-summary/v1"
                    and result.text_commit_version == "provider-finished-text/v1"
                )
                extra = (
                    (
                        (
                            "context.compaction_completed"
                            if updated.phase == RunPhase.PREPARING_CONTEXT
                            else (
                                (
                                    "context.compaction_replanned"
                                    if updated.force_compaction and not snapshot.force_compaction
                                    else (
                                        "context.compaction_retry_scheduled"
                                        if updated.model_errors > 0 or updated.provider_errors > 0
                                        else (
                                            "context.compaction_continuation_scheduled"
                                            if result.response_kind == "output_limit"
                                            and not bounded_summary
                                            else "context.compaction_chunk_completed"
                                        )
                                    )
                                )
                                if updated.phase == RunPhase.COMPACTING
                                else "context.compaction_failed"
                            )
                        ),
                        {
                            "reason": updated.reason,
                            "response_kind": result.response_kind,
                            "model_errors": updated.model_errors,
                            "provider_errors": updated.provider_errors,
                            **(
                                {
                                    "summary_completion_policy": "bounded-committed-summary/v1",
                                    "bounded_summary_text_accepted": (
                                        updated.phase in {
                                            RunPhase.COMPACTING, RunPhase.PREPARING_CONTEXT
                                        }
                                        and not updated.model_errors and not updated.provider_errors
                                        and not (updated.force_compaction
                                                 and not snapshot.force_compaction)
                                    ),
                                }
                                if bounded_summary else {}
                            ),
                            **(
                                {
                                    "reason": "context_compaction_validation_failed",
                                    "reduction_policy": SUMMARY_REDUCTION_POLICY,
                                    "previous_retained_messages": len(
                                        snapshot.compaction_request["retained_message_refs"]
                                    ),
                                    "retained_messages": len(
                                        updated.compaction_request["retained_message_refs"]
                                    ),
                                }
                                if updated.phase == RunPhase.COMPACTING
                                and updated.force_compaction and not snapshot.force_compaction
                                else {}
                            ),
                        },
                    ),
                )
            else:
                updated = reduce_model(snapshot, result, config)
                if (intent.payload.get("purpose") != "delivery_review"
                        and result.response_kind in {
                            "tool_calls", "final_candidate", "output_limit"
                        }
                        and (result.tool_calls or result.text.strip())):
                    updated = consume_hook_feedback(updated)
                    updated = updated.model_copy(update={"pending_child_observation_ids": ()})
                    extra = (*extra, *(("input.guidance_consumed", {
                        "input_id": input_id, "operation_key": intent.operation_key,
                        "goal_revision": snapshot.goal_revision,
                    }) for input_id in snapshot.pending_guidance_ids))
                    updated = updated.model_copy(update={"pending_guidance_ids": ()})
            delay = retry_delay(
                result,
                (
                    updated.provider_errors
                    if result.response_kind == "provider_retryable_error"
                    else updated.model_errors
                ),
                config,
            )
            updated = updated.model_copy(
                update={"retry_not_before": time() + delay if delay else 0}
            )
            if delay and not updated.phase.terminal:
                provider_retry = result.response_kind == "provider_retryable_error"
                attempt = updated.provider_errors if provider_retry else updated.model_errors
                maximum = (config.profile.max_provider_retries if provider_retry
                           else config.profile.max_retries)
                extra = (*extra, ("model.retry_scheduled", {
                    "failure_category": result.failure_kind or result.response_kind,
                    "next_attempt": attempt + 1,
                    "max_attempts": maximum + 1,
                    "delay_ms": int(delay * 1000),
                }))
            if result.failure_kind is not None:
                extra = (
                    *extra,
                    (
                        "model.provider_failure",
                        {
                            "request_id": result.request_id,
                            "attempt_id": result.attempt_id,
                            "response_kind": result.response_kind,
                            "failure_kind": result.failure_kind,
                            "provider_failure_category": result.provider_failure_category,
                            "provider_status_code": result.provider_status_code,
                            "gateway_error_code": result.gateway_error_code,
                        },
                    ),
                )
            extra = (*extra, sealed_model_usage_event(result))
        elif intent.kind == "tool":
            tool_result = ToolResult.model_validate(receipt.result)
            parallel_calls = intent.payload.get("calls")
            if intent.payload.get("parallel_readonly") is True:
                updated = reduce_parallel_readonly_tools(
                    snapshot,
                    tool_result,
                    parallel_calls,
                    delegated=intent.payload.get("parallel_delegation") is True,
                    definitions=self.configuration.tools,
                    observation_policy=self.configuration.workspace_observation_policy,
                )
            else:
                tool_name = snapshot.pending_tools[0]["name"]
                definition = next(
                    (
                        definition
                        for definition in self.configuration.tools
                        if definition.name == tool_name
                    ),
                    None,
                )
                updated = reduce_tool(
                    snapshot, tool_result,
                    observation_role=definition.observation_role if definition else "none",
                    definition=definition,
                    observation_policy=self.configuration.workspace_observation_policy,
                )
            if config.hooks:
                calls = tuple(parallel_calls) if intent.payload.get("parallel_readonly") is True \
                    else (intent.payload["call"],)
                results = tuple(ToolResult.model_validate(row) for row in
                                tool_result.content["batch_results"]) \
                    if intent.payload.get("parallel_readonly") is True else (tool_result,)
                updated = stage_post_hooks(snapshot, updated, config, calls, results)
            if updated.phase == RunPhase.AWAITING_INTERACTION:
                if updated.child_approval is not None:
                    from .child_approval import approval_event
                    extra = (approval_event(updated),)
                else:
                    extra = (
                        (
                            "approval.requested",
                            {
                                "approval_id": updated.pending_interaction,
                                "call_id": snapshot.pending_tools[0]["call_id"],
                                "kind": "tool",
                                "detail": {
                                    "tool_name": snapshot.pending_tools[0]["name"],
                                    "arguments": snapshot.pending_tools[0]["arguments"],
                                },
                            },
                        ),
                    )
            # Ledger collections are idempotent across distinct query calls.
            # Emit only the same actual delta consumed by the reducer.
            if tool_result.child_budget_settlement is not None:
                from .child_budget import apply_child_budget

                _, usage = apply_child_budget(snapshot, tool_result, definition)
            else:
                usage = tool_result.resource_usage
            if usage.child_run_ids or usage.model_calls:
                known = all(
                    value is not None
                    for value in (
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cached_input_tokens,
                        usage.reasoning_tokens,
                    )
                )
                source = "descendant_runs" if usage.child_run_ids else "tool_model_effects"
                suffix = ":child-accounting" if usage.child_run_ids else ":model-accounting"
                extra = (
                    *extra,
                    (
                        "usage.reported" if known else "usage.unavailable",
                        {
                            "semantics": "delta",
                            "request_id": intent.operation_key,
                            "attempt_id": intent.operation_key + suffix,
                            "source": source,
                            "child_run_ids": list(usage.child_run_ids),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cached_input_tokens": usage.cached_input_tokens,
                            "reasoning_tokens": usage.reasoning_tokens,
                        },
                    ),
                )
            if (
                not call_room(config.limits.max_model_calls, updated.model_budget_claimed, 0)
                or not call_room(config.limits.max_tool_calls, updated.tool_budget_claimed, 0)
            ):
                updated = finish(updated, "blocked", "tool_resource_budget_violation")
        else:
            try:
                updated = reduce_verification(
                    snapshot, VerificationBundle.model_validate(receipt.result), config
                )
            except ValueError:
                updated = finish(snapshot, "unverified", "verification_protocol_error")
        if interruption is not None:
            # A known child receipt proves its own usage, not that the outer
            # interrupted effect completed or that the parent may continue.
            wall_expired = interruption.result.get("reason") == "wall_time_hard_limit"
            updated = finish(updated, "budget_exhausted" if wall_expired else "unverified",
                             "wall_time_hard_limit" if wall_expired else "effect_outcome_unknown")
            extra = (*extra, ("effect.interrupted", {
                "operation_key": intent.operation_key,
                "reason": interruption.result.get("reason", "effect_outcome_unknown"),
            }))
            if wall_expired or interruption.result.get("deadline_cause") == "wall_time_hard_limit":
                extra = (*extra, ("budget.exhausted", {"reason": "wall_time_hard_limit"}))
        if updated.phase.terminal:
            extra = (
                *extra,
                (
                    f"run.{updated.phase.value}",
                    {"outcome": updated.outcome, "reason": updated.reason},
                ),
            )
        if sealed_model_response is not None:
            from .model_input import input_event

            observed = sealed_model_response.input_receipt
            if (observed is not None and observed.profile_digest == config.profile.digest
                    and observed != snapshot.model_input_observation):
                extra = (*extra, input_event(observed))
        received_payload = {
            "operation_key": intent.operation_key,
            "kind": intent.kind,
            "status": receipt.status,
        }
        if reconciled:
            received_payload["recovery_policy"] = config.tool_recovery_policy
        if sealed_model_response is not None:
            received_payload.update(
                model_response_kind=sealed_model_response.response_kind,
                model_tool_call_count=len(sealed_model_response.tool_calls),
                model_text_chars=len(sealed_model_response.text),
            )
        if intent.kind == "model":
            received_payload["model_purpose"] = intent.payload.get("purpose", "main")
            # Freeze display semantics in the committed occurrence. A new
            # reader must not alter an old canonical event's stable identity
            # and payload merely because projection implementation upgraded.
            received_payload["provisional_display_policy"] = "native.provisional-display/v1"
        if intent.kind == "tool":
            tool_status = receipt.result.get("status")
            if tool_status in {"ok", "error", "uncertain", "approval_required"}:
                # Persist the trusted contract enum, never arbitrary tool text.
                received_payload["tool_result_status"] = tool_status
            raw_calls = intent.payload.get("calls")
            if not isinstance(raw_calls, list):
                raw_calls = [intent.payload.get("call")]
            if receipt.status == "committed":
                tool_result = ToolResult.model_validate(receipt.result)
                batch = (
                    tool_result.content.get("batch_results")
                    if isinstance(tool_result.content, dict)
                    else None
                )
                raw_results = batch if isinstance(batch, list) else [receipt.result]
            else:
                raw_results = []
            received_payload["tool_results"] = [
                {
                    "call_id": call["call_id"],
                    **({"tool_display": result["public_display"]}
                       if isinstance(result, dict) and result.get("public_display") else {}),
                    "status": (
                        result["status"]
                        if isinstance(result, dict)
                        and result.get("status")
                        in {"ok", "error", "uncertain", "approval_required"}
                        else "uncertain"
                    ),
                }
                for call, result in zip(raw_calls, raw_results, strict=False)
                if isinstance(call, dict) and isinstance(call.get("call_id"), str)
            ]
        receipts = [receipt]
        if intent.payload.get("parallel_readonly") is True:
            for index, _call in enumerate(intent.payload["calls"], 1):
                child_key = f"{intent.operation_key}:call:{index}"
                if (self.store.get_intent(self.scope, snapshot.run_id, child_key) is not None
                        and self.store.get_receipt(self.scope, snapshot.run_id, child_key) is None):
                    receipts.append(
                        EffectReceipt(
                            operation_key=child_key,
                            status="uncertain",
                            result={"reason": "parallel_child_not_settled"},
                        )
                    )
        batch = commit_transition(
            snapshot,
            updated,
            "effect.received",
            received_payload,
            receipts=tuple(receipts),
            extra_events=extra,
        )
        from .tool_interruption import record_tool_interruption

        committed = self._commit(
            snapshot, record_tool_interruption(snapshot, batch, intent, receipt)
        )
        if (intent.kind == "workspace" and intent.payload.get("purpose") == "finalization"
                and intent.payload.get("fencing_token") ==
                self._finalization_recovery_fences.get(snapshot.run_id)):
            # Only the successfully committed check for this owner discharges
            # its guard. Older prepared/running intents cannot certify a new fence.
            self._finalization_recovery_fences.pop(snapshot.run_id, None)
        return committed
