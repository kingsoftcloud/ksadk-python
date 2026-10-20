"""Pure runtime decisions. The Store is the sole owner of committed state."""

from __future__ import annotations

import hashlib
import json

from .call_budget import call_room
from .completion_text import complete_continuation, fragment_digest
from .context_continuation import ContextRuntimeError, restart_continuation
from .delivery_review import delivery_review_readers, reduce_delivery_review, review_pending
from .final_output import normalize_final_output
from .final_submission import FINAL_SUBMISSION_TOOL, submitted_final_text
from .guidance import apply_guidance
from .interaction_observation import advance_browser_observation, advance_interaction_observation
from .model import ModelTurnResult
from .model_feedback import model_feedback_messages
from .model_replay import replay_binding_failure, replay_fields
from .model_tool_batch import begin_tool_batch
from .output_recovery import output_recovery_mode
from .ports import (
    ArtifactRef,
    RunConfiguration,
    ToolResult,
    WorkspaceObservation,
)
from .state import (
    CommitBatch,
    RunCommand,
    RunPhase,
    RunSnapshot,
    move,
    workspace_observation_cursor,
)
from .steering import apply_steering
from .task_progress import TODO_WRITE_NAME, parse_task_progress
from .terminal_verification import prepare_terminal_verification
from .tool_budget import (
    dispatch_tool_budget,
    finalization_only,
    model_tool_surface,
    model_turn_control,
    refuse_pending_batch,
    refused_pending_tools,
    tools_exhausted,
)
from .tool_error_recovery import (
    advance_diagnostic_recovery,
    recovery_boundary,
    refuse_undiagnosed_action,
    reserve_diagnostic_trial,
)
from .tool_error_streak import advance_tool_error_streak, tool_error_streak_exhausted
from .tool_hook_runtime import (
    hook_execution_metadata,
    prepare_tool_hook,
    tool_hook_feedback_messages,
)
from .tool_observation import (
    append_executed_tool_message,
    executed_tool_message,
    parallel_result_violation,
)
from .tool_progress import tool_progress_feedback
from .tool_scheduling import scheduled_tool_calls
from .transitions import commit_transition, finish, prepare_effect
from .user_input import current_user_has_media
from .verification_workspace import changed_verification_subject


def reduce_command(
    snapshot: RunSnapshot,
    command: RunCommand,
    *,
    bound_tool_names: frozenset[str] | None = None,
    guidance_enabled: bool = False,
    replay_configuration: RunConfiguration | None = None,
) -> CommitBatch:
    kind, updated, reason = command.kind, snapshot, "applied"
    extra = ()
    if kind == "cancel":
        updated = move(snapshot, RunPhase.CANCELED).model_copy(
            update={
                "outcome": "unverified",
                "reason": "user_canceled",
                "pending_intent": None,
                "pending_interaction": None,
                "interaction_host_id": None,
                "interaction": None,
                "child_approval": None,
                "pending_tools": (),
            }
        )
    elif kind == "policy_revoked":
        # A revoked Run cannot enter steerable finalization or promote its
        # previous candidate. Its accepted inbox is settled by the Store.
        updated = move(snapshot, RunPhase.FAILED).model_copy(
            update={
                "outcome": "blocked",
                "reason": "policy_revoked",
                "pending_intent": None,
                "pending_interaction": None,
                "interaction_host_id": None,
                "interaction": None,
                "child_approval": None,
                "pending_tools": (),
                "final_text": "",
            }
        )
    elif kind == "pause":
        if snapshot.phase == RunPhase.PAUSED:
            reason = "already_paused"
        else:
            updated = move(snapshot, RunPhase.PAUSED)
    elif kind == "resume" and snapshot.phase == RunPhase.PAUSED:
        updated = move(snapshot, snapshot.paused_from or RunPhase.PREPARING_CONTEXT)
    elif kind == "submit_interaction" and snapshot.phase == RunPhase.AWAITING_INTERACTION:
        if command.payload.get(
            "interaction_id"
        ) != snapshot.pending_interaction or command.payload.get("decision") not in {
            "allow",
            "deny",
        }:
            reason = "interaction_mismatch"
        else:
            updated = move(snapshot, snapshot.resume_phase or RunPhase.EXECUTING_TOOLS).model_copy(
                update={
                    "interaction": {
                        "interaction_id": snapshot.interaction_host_id,
                        "decision": command.payload["decision"],
                    },
                    "pending_interaction": None,
                }
            )
            extra = (
                (
                    "approval.resolved",
                    {
                        "approval_id": snapshot.pending_interaction,
                        "call_id": (
                            snapshot.child_approval.tool_call.call_id
                            if snapshot.child_approval
                            else snapshot.pending_tools[0]["call_id"]
                        ),
                        "decision": command.payload["decision"],
                    },
                ),
            )
    elif kind == "steer":
        updated, reason = apply_steering(snapshot, command, bound_tool_names=bound_tool_names)
        if reason == "applied" and command.payload.get("task_contract") is not None:
            from .context_replay import reset_steered_replay

            updated, replay_reset = reset_steered_replay(snapshot, updated, replay_configuration)
            if replay_reset is not None:
                extra = (("context.replay_root_reset", replay_reset),)
    elif kind == "guide":
        if guidance_enabled:
            updated, reason = apply_guidance(snapshot, command)
        else:
            reason = "guidance_policy_unavailable"
        if reason == "applied":
            extra = (("input.guidance_applied", {
                "command_id": command.command_id, "input_id": command.command_id,
                "goal_revision": snapshot.goal_revision, "delivery": "guide",
            }),)
    else:
        reason = "command_not_applicable"
    if kind in {"steer", "guide"} and reason == "applied":
        from .operator_guidance import bind_operator_input_history

        updated, operator_input = bind_operator_input_history(
            snapshot, updated, replay_configuration, command.command_id,
        )
        if operator_input is not None:
            extra = (*extra, ("input.operator_guidance_superseded", operator_input))
    if (
        kind in {"cancel", "policy_revoked", "steer"}
        and snapshot.pending_interaction
        and updated.pending_interaction is None
    ):
        extra = (*extra,
            (
                "approval.resolved",
                {
                    "approval_id": snapshot.pending_interaction,
                    "call_id": (
                        snapshot.child_approval.tool_call.call_id
                        if snapshot.child_approval
                        else snapshot.pending_tools[0]["call_id"]
                    ),
                    "decision": "canceled",
                    "reason": kind,
                },
            ),
        )
    if snapshot.pending_guidance_ids and (
        kind in {"cancel", "policy_revoked"} or kind == "steer" and reason == "applied"
    ):
        extra = (*extra, *(("input.guidance_discarded", {
            "input_id": input_id, "reason": kind,
        }) for input_id in snapshot.pending_guidance_ids))
        updated = updated.model_copy(update={"pending_guidance_ids": ()})
    if kind == "cancel":
        extra = (*extra, ("run.canceled", {"outcome": updated.outcome, "reason": updated.reason}))
    elif kind == "policy_revoked":
        extra = (*extra, ("run.failed", {"outcome": updated.outcome, "reason": updated.reason}))
    return commit_transition(
        snapshot,
        updated,
        "command.consumed",
        {
            "command_id": command.command_id,
            "kind": kind,
            "reason": reason,
            "outcome": updated.outcome,
        },
        command_id=command.command_id,
        extra_events=extra,
    )


def host_resolved_candidate(snapshot: RunSnapshot, config: RunConfiguration) -> CommitBatch | None:
    """Submit a trusted host answer once, through the ordinary verifier path.

    Goal text and revision both pin the answer: steering under an unchanged
    contract cannot reuse it. A failed candidate enters ordinary model repair.
    Command precedence and CAS remain the driver's and Store's responsibility.
    """
    from .stop_hook_runtime import stop_hook_state

    answer = config.contract.host_resolved_answer
    if (
        answer is None
        # Inline guidance preserves the original goal/contract, but the old
        # host answer cannot attest to the newly admitted user requirement.
        or snapshot.pending_guidance_ids
        or current_user_has_media(snapshot.messages, snapshot.goal)
        or snapshot.phase != RunPhase.PREPARING_CONTEXT
        or snapshot.goal_revision != answer.goal_revision
        or hashlib.sha256(snapshot.goal.encode()).hexdigest() != answer.goal_digest
        or snapshot.model_calls
        or snapshot.tool_calls
        or snapshot.repair_attempts
        or stop_hook_state(snapshot).continuation_count
        or config.contract.allowed_tool_names != ()
        or config.final_submission_schema is not None
        or config.final_output_format != "text"
    ):
        return None
    candidate = {
        "candidate_id": f"{snapshot.run_id}:host-candidate:{snapshot.goal_revision}",
        "final_text": answer.text,
    }
    updated = move(snapshot, RunPhase.VERIFYING).model_copy(
        update={"candidate": candidate, "prepared_context": None}
    )
    return commit_transition(
        snapshot,
        updated,
        "completion.host_candidate",
        {
            "authority": answer.authority,
            "evidence_ref": answer.evidence_ref,
            "goal_revision": snapshot.goal_revision,
            "answer_digest": hashlib.sha256(answer.text.encode()).hexdigest(),
        },
    )


def next_transition(snapshot: RunSnapshot, config: RunConfiguration) -> CommitBatch | None:
    phase = snapshot.phase
    if phase.terminal or phase in {RunPhase.PAUSED, RunPhase.AWAITING_INTERACTION}:
        return None
    if phase == RunPhase.CREATED:
        return commit_transition(
            snapshot,
            move(snapshot, RunPhase.PREPARING_CONTEXT),
            "run.started" if snapshot.event_seq == 0 else "run.phase_changed",
        )
    if phase == RunPhase.PREPARING_CONTEXT:
        if recovery := recovery_boundary(snapshot, config):
            return recovery
        if resolved := host_resolved_candidate(snapshot, config):
            return resolved
        if not call_room(config.limits.max_model_calls, snapshot.model_budget_claimed):
            updated = finish(snapshot, "budget_exhausted", "model_budget_exhausted")
        else:
            updated = move(snapshot, RunPhase.REQUESTING_MODEL)
        return commit_transition(
            snapshot, updated, "run.phase_changed", {"phase": updated.phase.value}
        )
    if phase == RunPhase.REQUESTING_MODEL:
        payload = {"model_call": snapshot.model_calls + 1}
        if review_pending(snapshot):
            payload["purpose"] = "delivery_review"
        elif control := model_turn_control(snapshot, config):
            payload["purpose"] = "finalization" if control.finalization_only else "task"
            payload["turn_control"] = control.model_dump(mode="json")
        from .model_request import capture_request_context

        return capture_request_context(snapshot, prepare_effect(snapshot, "model", payload), config)
    if phase == RunPhase.EXECUTING_TOOLS:
        if refused := refuse_undiagnosed_action(snapshot):
            return refused
        if hook := prepare_tool_hook(snapshot, config):
            return hook
        if not snapshot.pending_tools:
            if recovery := recovery_boundary(snapshot, config):
                return recovery
            if tool_error_streak_exhausted(snapshot) and snapshot.tool_error_recovery is None:
                updated = finish(snapshot, "blocked", "repeated_tool_error_no_progress")
                return commit_transition(
                    snapshot,
                    updated,
                    "tools.completed",
                    {"reason": "repeated_tool_error_no_progress"},
                )
            feedback = tool_progress_feedback(
                tool_hook_feedback_messages(snapshot, snapshot.messages)
            )
            updated = move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
                update={"messages": feedback.messages}
            )
            return commit_transition(snapshot, updated, "tools.completed")
        if tools_exhausted(snapshot, config):
            return refuse_pending_batch(snapshot)
        # ZCode schedules explicitly concurrent-safe tool calls in a bounded
        # parallel group. Keep the same conservative rule here: only a
        # contiguous prefix of host-declared, read-only calls can share one
        # effect. Writes, browser actions and approval-capable tools stay in
        # the ordinary per-call ledger path.
        parallel = scheduled_tool_calls(snapshot, config)
        if len(parallel) > 1:
            definition = next(tool for tool in config.tools if tool.name == parallel[0]["name"])
            payload = {
                "calls": list(parallel), "parallel_readonly": True,
                "parallel_delegation": definition.delegated_read_only,
            }
            delegated = definition.delegated_read_only
            calls = parallel
        else:
            calls = (snapshot.pending_tools[0],)
            payload = {"call": calls[0]}
            definition = next((tool for tool in config.tools
                               if tool.name == calls[0]["name"]), None)
            delegated = definition is not None and definition.child_budget_policy in {
                "delegate/v1", "start/v1",
            }
        if config.child_lifecycle_policy == "owned-background/v2" and delegated:
            # Preserve the actual admission before any member reports usage
            # or reserves a child. Cancel/resume must not recalculate a grant
            # from the later, partly charged parent snapshot.
            payload["child_dispatch_budgets"] = [
                dispatch_tool_budget(snapshot, config, batch_size=len(calls), index=index)
                .model_dump(mode="json") for index in range(len(calls))
            ]
        return reserve_diagnostic_trial(snapshot, prepare_effect(snapshot, "tool", payload))
    if phase == RunPhase.VERIFYING:
        if any(child.settled_usage_digest is None for child in snapshot.child_budget_reservations):
            return commit_transition(
                snapshot, finish(snapshot, "unverified", "background_children_unsettled"),
                "child.delivery_unsettled", {
                    "child_run_ids": [
                        child.child_run_id for child in snapshot.child_budget_reservations
                        if child.settled_usage_digest is None
                    ],
                },
            )
        if snapshot.browser_reobservation_required:
            # Every final protocol reaches this gate, including structured
            # final submission. Model prose cannot clear resource continuity.
            return commit_transition(
                snapshot, move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(update={
                    "candidate": None, "verification": None,
                    "messages": (*snapshot.messages, {
                        "role": "user", "kind": "browser_recovery_observation",
                        "content": json.dumps({"fresh_observation_required": True,
                                               "candidate_accepted": False}),
                    }),
                }), "browser.candidate_rejected", {"reason": "fresh_observation_required"},
            )
        from .stop_hook_runtime import prepare_stop_hook

        return prepare_stop_hook(snapshot, config) or prepare_effect(
            snapshot, "verify", snapshot.candidate or {}
        )
    if phase == RunPhase.FINALIZING:
        qualified = prepare_terminal_verification(snapshot, config)
        if qualified.reason in {"effect_outcome_unknown", "browser_session_unavailable"}:
            terminal = RunPhase.INTERRUPTED
        elif qualified.outcome == "failed" or not qualified.final_text.strip():
            # A terminal without deliverable text cannot be a completed turn.
            # Studio applies the same nonempty-delivery gate to Native runs.
            terminal = RunPhase.FAILED
        else:
            terminal = RunPhase.COMPLETED
        updated = move(qualified, terminal)
        extra = ()
        if snapshot.final_text:
            extra = (("text.completed", {"text": snapshot.final_text, "phase": "final_answer"}),)
        report_ref = None
        if updated.verification:
            report_ref = f"verification:{updated.run_id}:{updated.verification['candidate_id']}"
        # Only the committed finalization emits final text; a model candidate is
        # never projected as an accepted user answer.
        return commit_transition(
            snapshot,
            updated,
            "run.finalizing",
            extra_events=(
                *extra,
                (
                    f"run.{terminal.value}",
                    {
                        "outcome": updated.outcome,
                        "reason": updated.reason,
                        "contract_revision": config.contract.revision,
                        "verification_report_ref": report_ref,
                        "verification": updated.verification,
                    },
                ),
            ),
        )
    raise ValueError(f"unsupported phase: {phase.value}")


def reduce_model(
    snapshot: RunSnapshot, result: ModelTurnResult, config: RunConfiguration
) -> RunSnapshot:
    from .model_input import input_binding_failure, observe_input

    kind = result.response_kind
    updated = snapshot.model_copy(
        update={
            "model_calls": snapshot.model_calls + 1,
            "pending_intent": None,
            "provider_errors": (
                snapshot.provider_errors + 1 if kind == "provider_retryable_error" else 0
            ),
        }
    )
    if failure := input_binding_failure(config.profile, result.input_receipt):
        return finish(updated, "failed", failure)
    updated = observe_input(updated, result.input_receipt)
    if (config.model_recovery_policy == "pure-inference-takeover/v1"
            and kind == "provider_retryable_error"):
        updated = updated.model_copy(update={"model_recovery_retries_spent": max(
            snapshot.model_recovery_retries_spent, snapshot.provider_errors
        ) + 1})
    if failure := replay_binding_failure(
        config.profile, result.provider_replay,
        required=kind in {"tool_calls", "final_candidate", "output_limit"},
    ):
        return finish(updated, "failed", failure)
    if (result.provider_replay is not None
            and config.profile.provider_replay_projection_policy
            == "anthropic-client-compaction/v1"):
        from .model_request import model_request
        from .replay_context import bind_replay_input

        request = model_request(snapshot, config, key=result.request_id, payload={
            "purpose": "delivery_review" if review_pending(snapshot) else "task",
        })
        result = bind_replay_input(result, request)
    if review_pending(snapshot) and kind == "tool_calls":
        # Admit the surface advertised for this request. Charging its model
        # call must not revoke an already offered source read; the next
        # request uses the updated budget and read-round count.
        readers = {tool.name for tool in delivery_review_readers(snapshot, config)}
        if readers and all(call.name in readers for call in result.tool_calls):
            # Same tool ledger and permissions; only existing host-declared
            # source readers are admitted. Private review prose stays private.
            updated = updated.model_copy(update={
                "delivery_review": snapshot.delivery_review.model_copy(update={
                    "read_rounds": snapshot.delivery_review.read_rounds + 1,
                    "continuation_messages": (),
                }),
            })
            sealed = begin_tool_batch(snapshot, updated, result)
            return sealed.model_copy(update={"messages": (
                *sealed.messages[:-1],
                {**sealed.messages[-1], "kind": "delivery_review_source"},
            )})
    if review_pending(snapshot) and kind not in {
        "provider_retryable_error", "policy_abort", "user_abort", "context_overflow",
        "provider_terminal_error",
    }:
        return reduce_delivery_review(updated, result, config)
    if kind == "tool_calls":
        if config.final_submission_schema is not None and any(
            call.name == FINAL_SUBMISSION_TOOL for call in result.tool_calls
        ):
            if len(result.tool_calls) == 1:
                submitted = submitted_final_text(result.tool_calls[0], config)
                if submitted is not None:
                    return move(updated, RunPhase.VERIFYING).model_copy(
                        update={
                            "candidate": {
                                "candidate_id": (
                                    f"{snapshot.run_id}:candidate:{updated.model_calls}"
                                ),
                                "final_text": submitted,
                            },
                            "candidate_replay": result.provider_replay,
                            "model_errors": 0,
                            "continuations": 0,
                        }
                    )
            # The internal submission must be a single valid call. Seal all
            # model-declared calls with non-execution receipts; never dispatch
            # a mixed batch or allow its ordinary tools to cause effects.
            errors = snapshot.model_errors + 1
            declared = begin_tool_batch(snapshot, updated, result)
            refused = move(declared, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "pending_tools": (),
                    "model_errors": errors,
                    "messages": (
                        *declared.messages,
                        *refused_pending_tools(
                            declared, reason="final_submission_schema_or_batch_invalid"
                        ),
                    ),
                }
            )
            if errors > config.profile.max_retries:
                return finish(refused, "unverified", "final_submission_retry_exhausted")
            return refused
        if tools_exhausted(snapshot, config):
            return finish(updated, "budget_exhausted", "tool_call_after_budget_exhaustion")
        if finalization_only(snapshot, config):
            return finish(updated, "budget_exhausted", "tool_call_after_finalization_reserve")
        from .delivery_artifacts import delivery_read_outside_paths

        if delivery_read_outside_paths(snapshot, config, result.tool_calls):
            declared = begin_tool_batch(snapshot, updated, result)
            return move(declared, RunPhase.PREPARING_CONTEXT).model_copy(update={
                "pending_tools": (),
                "messages": (*declared.messages, *refused_pending_tools(
                    declared, reason="delivery_read_path_not_written",
                )),
            })
        advertised = {tool.name for tool in model_tool_surface(snapshot, config)}
        if any(call.name not in advertised for call in result.tool_calls):
            # Never execute a batch containing a call outside current admission. Pair all
            # sealed calls with non-execution receipts so the model can correct
            # an occasional invented name, but bound consecutive violations.
            # A known tool withheld for this turn does not consume that retry
            # allowance; catalog visibility does not grant execution authority.
            bound = {tool.name for tool in config.tools}
            if any(call.name not in bound for call in result.tool_calls):
                errors = snapshot.model_errors + 1
                declared = begin_tool_batch(snapshot, updated, result)
                refused = move(declared, RunPhase.PREPARING_CONTEXT).model_copy(
                    update={
                        "pending_tools": (),
                        "model_errors": errors,
                        "messages": (
                            *declared.messages,
                            *refused_pending_tools(declared, reason="tool_not_bound"),
                        ),
                    }
                )
                if errors > config.profile.max_retries:
                    return finish(refused, "blocked", "tool_call_not_advertised_retry_exhausted")
                return refused
            declared = begin_tool_batch(snapshot, updated, result)
            return move(declared, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "pending_tools": (),
                    "messages": (
                        *declared.messages,
                        *refused_pending_tools(
                            declared,
                            reason="tool_unavailable_in_current_turn",
                            available_tools=tuple(sorted(advertised)),
                        ),
                    ),
                }
            )
        return begin_tool_batch(snapshot, updated, result)
    if kind == "final_candidate":
        if config.final_submission_schema is not None:
            errors = snapshot.model_errors + 1
            if errors > config.profile.max_retries or (
                not call_room(config.limits.max_model_calls, updated.model_budget_claimed)
            ):
                return finish(updated, "unverified", "final_submission_missing")
            # The provider completed a real assistant step, but used ordinary
            # text instead of the host-owned submission tool.  Keep that exact
            # draft in the same turn so the next request can repair only the
            # delivery envelope.  The typed kind keeps it out of public Session
            # history and it remains explicitly rejected, never a candidate.
            draft = {
                "role": "assistant",
                "kind": "unstructured_final_draft",
                "content": result.text,
                "candidate_status": "rejected",
                "request_id": result.request_id,
                "attempt_id": result.attempt_id,
                **replay_fields(result.provider_replay),
            }
            return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "model_errors": errors,
                    "messages": (
                        *snapshot.messages,
                        draft,
                        {
                            "role": "user",
                            "kind": "final_submission_required",
                            "content": (
                                "The final answer must be submitted through the "
                                "submit_final_output tool matching its schema."
                            ),
                        },
                    ),
                }
            )
        final_text = normalize_final_output(
            complete_continuation(snapshot.messages, result.text),
            output_format=config.final_output_format,
        )
        return move(updated, RunPhase.VERIFYING).model_copy(
            update={
                "candidate": {
                    "candidate_id": f"{snapshot.run_id}:candidate:{updated.model_calls}",
                    "final_text": final_text,
                },
                "candidate_replay": result.provider_replay,
                "model_errors": 0,
            }
        )
    if kind == "output_limit":
        if snapshot.continuations >= config.profile.max_continuations:
            if snapshot.output_restarts < config.limits.max_output_restarts:
                try:
                    messages, history = restart_continuation(snapshot, result.text)
                except ContextRuntimeError as exc:
                    return finish(updated, "blocked", str(exc))
                return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
                    update={
                        "continuations": 0,
                        "output_restarts": snapshot.output_restarts + 1,
                        "messages": messages,
                        "compaction_history": history,
                    }
                )
            return finish(updated, "unverified", "continuation_budget_exhausted")
        messages = snapshot.messages
        recovery_mode = output_recovery_mode(updated, config) if result.text else "empty_output"
        if result.text:
            response_id = f"{result.request_id}:{result.attempt_id}"
            digest = fragment_digest(result.text)
            messages = (
                *messages,
                {
                    "role": "assistant",
                    "kind": "continuation_fragment",
                    "content": result.text,
                    **replay_fields(result.provider_replay),
                    "continuation_response_id": response_id,
                },
            )
        else:
            response_id = f"{result.request_id}:{result.attempt_id}"
            digest = fragment_digest("")
        return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
            update={
                "continuations": snapshot.continuations + 1,
                "messages": (
                    *messages,
                    {
                        "role": "user",
                        "kind": "continuation_observation",
                        "content": (
                            "The previous output was interrupted while task tools remain "
                            "available. It is work in progress, not a completed delivery. "
                            "Continue from the last committed state using the available tools "
                            "as needed; break remaining work into smaller actions. Incomplete "
                            "tool calls were not executed. Give a fresh final answer only after "
                            "the task is complete."
                            if recovery_mode == "task_resume"
                            else
                            "The last response hit its output limit. Return only the exact "
                            "missing suffix of that response. Incomplete tool calls were not "
                            "executed."
                            if result.text
                            else "The last response hit its output limit without visible content "
                            "or a committed tool call. Continue the task from the last committed "
                            "state; take the next useful action or answer only if the task is "
                            "complete."
                        ),
                        "continuation_mode": recovery_mode,
                        "continuation_response_id": response_id,
                        "continuation_fragment_digest": digest,
                    },
                ),
            }
        )
    if kind in {"empty_response", "malformed_tool_call", "provider_retryable_error"}:
        provider_error = kind == "provider_retryable_error"
        errors = updated.provider_errors if provider_error else snapshot.model_errors + 1
        if provider_error and config.model_recovery_policy == "pure-inference-takeover/v1":
            errors = max(errors, updated.model_recovery_retries_spent)
        updated = updated.model_copy(
            update={
                "model_errors": 0 if provider_error else errors,
                "messages": model_feedback_messages(
                    snapshot, result, tools_available=bool(model_tool_surface(snapshot, config))
                ),
            }
        )
        limit = (
            config.profile.max_provider_retries if provider_error else config.profile.max_retries
        )
        if errors > limit:
            # No sealed final candidate exists. Report a failed ledger terminal
            # so the native state agrees with the Studio delivery failure.
            return move(updated, RunPhase.FAILED).model_copy(
                update={"outcome": "failed", "reason": f"{kind}_retry_exhausted"}
            )
        return move(updated, RunPhase.PREPARING_CONTEXT)
    if kind == "policy_abort":
        return finish(updated, "blocked", "model_policy_abort")
    if kind == "user_abort":
        return move(updated, RunPhase.CANCELED).model_copy(
            update={"outcome": "unverified", "reason": "user_canceled"}
        )
    if kind == "context_overflow":
        if snapshot.context_binding and snapshot.context_overflows < config.profile.max_retries:
            return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "force_compaction": True,
                    "prepared_context": None,
                    "context_overflows": snapshot.context_overflows + 1,
                }
            )
        return finish(updated, "blocked", "context_compaction_unavailable")
    return move(updated, RunPhase.FAILED).model_copy(
        update={"outcome": "failed", "reason": "provider_terminal_error"}
    )


def reduce_tool(
    snapshot: RunSnapshot,
    result: ToolResult,
    *,
    observation_role: str = "none",
    definition=None,
    observation_policy=None,
) -> RunSnapshot:
    from .child_budget import apply_child_budget
    from .observed_progress import advance_observed_progress
    from .task_policy import invalidates_browser_observation

    updated = snapshot.model_copy(update={"pending_intent": None})
    if result.status == "approval_required":
        if result.child_approval is not None:
            from .child_approval import host_id, stage_child_approval

            if (definition is None or definition.child_approval_policy is None
                    or result.interaction_id != host_id(result.child_approval)):
                raise ValueError("child_approval_authority_mismatch")
            return stage_child_approval(updated, result.child_approval,
                                        operation_key=snapshot.pending_intent,
                                        resume_phase=RunPhase.EXECUTING_TOOLS)
        return move(updated, RunPhase.AWAITING_INTERACTION).model_copy(
            update={
                "resume_phase": RunPhase.EXECUTING_TOOLS,
                "pending_interaction": f"{snapshot.pending_intent}:approval",
                "interaction_host_id": result.interaction_id,
                "interaction": None,
            }
        )
    from .background_commands import remember_command

    updated = updated.model_copy(update={
        "background_commands": remember_command(snapshot, result, definition),
    })
    budget_snapshot, usage = apply_child_budget(snapshot, result, definition)
    updated = updated.model_copy(update={"child_approval": None})
    updated = updated.model_copy(update={
        "child_budget_reservations": budget_snapshot.child_budget_reservations,
    })
    updated = updated.model_copy(
        update={
            "tool_calls": snapshot.tool_calls + 1,
            "descendant_model_calls": snapshot.descendant_model_calls + usage.model_calls,
            "descendant_tool_calls": snapshot.descendant_tool_calls + usage.tool_calls,
            "child_run_ids": tuple(dict.fromkeys(
                (*budget_snapshot.child_run_ids, *usage.child_run_ids)
            )),
        }
    )
    if observation_role == "browser" and result.browser_session_ref is not None:
        from .browser_recovery import remember_browser

        updated = remember_browser(
            updated, result.browser_session_ref, closed=result.browser_session_closed
        )
        if updated.phase == RunPhase.FINALIZING:
            return updated
    if result.status == "uncertain":
        return finish(updated, "unverified", "effect_outcome_unknown")
    message = executed_tool_message(
        result, call_id=snapshot.pending_tools[0]["call_id"], definition=definition,
        receipt_ref=snapshot.pending_intent, goal_revision=snapshot.goal_revision,
        tool_call=updated.tool_calls, hook_metadata=hook_execution_metadata(snapshot),
    )
    artifacts = {
        ref.uri: ref
        for ref in (ArtifactRef.model_validate(item) for item in snapshot.artifact_refs)
    }
    for ref in result.artifact_refs:
        previous = artifacts.get(ref.uri)
        if previous is not None and previous != ref:
            return finish(updated, "failed", "artifact_reference_conflict")
        artifacts[ref.uri] = ref
    from .delivery_artifacts import remember_delivery_write

    values = {
        "pending_tools": snapshot.pending_tools[1:],
        "delivery_artifacts": remember_delivery_write(snapshot, result, definition),
        "messages": append_executed_tool_message(snapshot.messages, message),
        "artifact_refs": tuple(ref.model_dump(mode="json") for ref in artifacts.values()),
        "workspace_modified_paths": tuple(
            dict.fromkeys((*snapshot.workspace_modified_paths, *result.workspace_changed_paths))
        )[:2048],
        "workspace_dirty_since_checkpoint": (
            snapshot.workspace_dirty_since_checkpoint or bool(result.workspace_changed_paths)
        ),
        "last_workspace_mutation_tool_call": (
            updated.tool_calls
            if result.workspace_changed_paths
            else snapshot.last_workspace_mutation_tool_call
        ),
        "last_web_mutation_tool_call": (
            updated.tool_calls
            if invalidates_browser_observation(result, observation_policy)
            else snapshot.last_web_mutation_tool_call
        ),
        "unresolved_interaction_observation": advance_interaction_observation(
            snapshot.unresolved_interaction_observation,
            role=observation_role,
            tool_name=snapshot.pending_tools[0]["name"],
            tool_call=updated.tool_calls,
            result=result,
        ),
        "last_browser_observation": advance_browser_observation(
            snapshot.last_browser_observation,
            role=observation_role,
            tool_call=updated.tool_calls,
            result=result,
        ),
        "last_browser_attempt_tool_call": (
            updated.tool_calls
            if observation_role == "browser"
            else snapshot.last_browser_attempt_tool_call
        ),
        "interaction": None,
        "interaction_host_id": None,
        **advance_tool_error_streak(snapshot, result, definition),
        **advance_observed_progress(snapshot, result, definition),
    }
    if snapshot.browser_reobservation_required and observation_role == "browser":
        from .browser_recovery import remaining_reobservations

        values["browser_reobservation_refs"] = remaining_reobservations(
            snapshot, result, values["last_browser_observation"]
        )
    if snapshot.pending_tools[0]["name"] == TODO_WRITE_NAME and result.status == "ok":
        try:
            values.update(
                task_progress=parse_task_progress(snapshot.pending_tools[0]["arguments"]),
                task_progress_revision=snapshot.task_progress_revision + 1,
            )
        except ValueError:
            # A conforming reserved port cannot return ok for an invalid plan.
            return finish(updated, "failed", "invalid_task_progress_receipt")
    values.update(advance_diagnostic_recovery(snapshot, result, definition, values))
    return updated.model_copy(update=values)


def reduce_parallel_readonly_tools(
    snapshot: RunSnapshot,
    result: ToolResult,
    calls: object,
    *,
    delegated: bool = False,
    definitions=(),
    observation_policy=None,
) -> RunSnapshot:
    """Commit ordered receipts from one bounded, read-only effect batch."""

    content = result.content
    rows = content.get("batch_results") if isinstance(content, dict) else None
    if (
        not isinstance(calls, list)
        or len(calls) < 2
        or len(snapshot.pending_tools) < len(calls)
        or tuple(snapshot.pending_tools[: len(calls)]) != tuple(calls)
    ):
        return finish(snapshot, "failed", "parallel_tool_receipt_invalid")

    def account_batch(current: RunSnapshot) -> RunSnapshot:
        # All members were admitted together. Early receipt rejection cannot
        # erase that cost or fabricate observations/descendant resource usage.
        # Keep per-member receipt indices assigned by reduce_tool unchanged.
        return current.model_copy(update={"tool_calls": snapshot.tool_calls + len(calls)})

    if not isinstance(rows, list) or len(rows) != len(calls):
        return account_batch(finish(snapshot, "failed", "parallel_tool_receipt_invalid"))
    updated = snapshot
    for index, raw in enumerate(rows):
        try:
            item = ToolResult.model_validate(raw)
        except ValueError:
            return account_batch(finish(updated, "failed", "parallel_tool_receipt_invalid"))
        usage = item.resource_usage
        if parallel_result_violation(item, delegated=delegated):
            return account_batch(finish(updated, "blocked", "parallel_readonly_contract_violation"))
        if delegated:
            call_id = updated.pending_tools[0]["call_id"]
            if call_id not in snapshot.parallel_child_settlements:
                return account_batch(finish(updated, "blocked", "parallel_child_receipt_missing"))
            reservation = item.child_budget_reservation
            if reservation is not None and (
                reservation.parent_operation_key != f"{snapshot.pending_intent}:call:{index + 1}"
                or reservation.parent_tool_call_id != call_id
                or reservation not in snapshot.child_budget_reservations
            ):
                return account_batch(finish(updated, "blocked", "parallel_child_receipt_mismatch"))
            # The member receipt already registered either its allocation or
            # its sealed usage before this batch completed. Publish the model
            # result here without applying that accounting for a second time.
            item = item.model_copy(update={
                "resource_usage": type(usage)(), "child_budget_reservation": None,
            })
        updated = reduce_tool(
            updated.model_copy(update={"pending_intent": snapshot.pending_intent}),
            item,
            definition=next(
                (d for d in definitions if d.name == updated.pending_tools[0]["name"]), None
            ),
            observation_policy=observation_policy,
        )
        if updated.phase.terminal or updated.phase == RunPhase.FINALIZING:
            return account_batch(updated)
    if delegated:
        updated = updated.model_copy(update={"parallel_child_settlements": ()})
    return updated


def reduce_workspace(
    snapshot: RunSnapshot,
    observation: WorkspaceObservation,
    cursor: str,
    config: RunConfiguration,
) -> RunSnapshot:
    purpose = "verification" if snapshot.phase == RunPhase.VERIFYING else "model"
    expected_cursor = workspace_observation_cursor(snapshot, purpose)
    if observation.operation_key != snapshot.pending_intent or cursor != expected_cursor:
        return finish(snapshot, "unverified", "workspace_observation_mismatch")
    if snapshot.phase == RunPhase.VERIFYING:
        changed = changed_verification_subject(
            snapshot,
            observation,
            config.limits.max_repairs,
            reason="candidate_workspace_changed",
        )
        if changed is not None:
            return changed
    revision_changed = (
        snapshot.tool_calls > 0
        and snapshot.workspace_revision is not None
        and observation.revision is not None
        and observation.revision != snapshot.workspace_revision
    )
    # Browser screenshots may be written into the visible workspace. They are
    # observations, not project edits; a fresh browser receipt at this cursor
    # must not schedule another source-verification checkpoint by itself.
    browser_artifact_only = (
        not observation.changed_paths
        and isinstance(snapshot.last_browser_observation, dict)
        and snapshot.last_browser_observation.get("tool_call") == snapshot.tool_calls
        and snapshot.last_workspace_mutation_tool_call != snapshot.tool_calls
    )
    values = {
        "pending_intent": None,
        "workspace_revision": observation.revision,
        "workspace_changed_paths": observation.changed_paths,
        "workspace_modified_paths": tuple(
            dict.fromkeys((*snapshot.workspace_modified_paths, *observation.changed_paths))
        )[:2048],
        "workspace_dirty_since_checkpoint": (
            snapshot.workspace_dirty_since_checkpoint
            or bool(observation.changed_paths)
            or (revision_changed and not browser_artifact_only)
        ),
        "last_workspace_mutation_tool_call": (
            snapshot.tool_calls
            if revision_changed and not browser_artifact_only
            else snapshot.last_workspace_mutation_tool_call
        ),
        "workspace_observation_status": observation.status,
        "workspace_observation_reason": observation.reason,
        "workspace_observation_cursor": cursor,
    }
    return snapshot.model_copy(update=values)
