"""Reduce provider model receipts into Native state without performing effects."""

from __future__ import annotations

from .call_budget import call_room
from .completion_text import complete_continuation, fragment_digest
from .context_continuation import ContextRuntimeError, restart_continuation
from .delivery_review import delivery_review_readers, reduce_delivery_review, review_pending
from .final_output import normalize_final_output
from .final_submission import FINAL_SUBMISSION_TOOL, submitted_final_text
from .model import ModelTurnResult
from .model_feedback import model_feedback_messages
from .model_replay import replay_binding_failure, replay_fields
from .model_tool_batch import begin_tool_batch
from .output_recovery import output_recovery_mode
from .ports import RunConfiguration
from .state import RunPhase, RunSnapshot, move
from .tool_budget import (
    finalization_only,
    model_tool_surface,
    refused_pending_tools,
    tools_exhausted,
)
from .transitions import finish


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
    if (
        config.model_recovery_policy == "pure-inference-takeover/v1"
        and kind == "provider_retryable_error"
    ):
        updated = updated.model_copy(
            update={
                "model_recovery_retries_spent": max(
                    snapshot.model_recovery_retries_spent, snapshot.provider_errors
                )
                + 1
            }
        )
    if failure := replay_binding_failure(
        config.profile,
        result.provider_replay,
        required=kind in {"tool_calls", "final_candidate", "output_limit"},
    ):
        return finish(updated, "failed", failure)
    if (
        result.provider_replay is not None
        and config.profile.provider_replay_projection_policy == "anthropic-client-compaction/v1"
    ):
        from .model_request import model_request
        from .replay_context import bind_replay_input

        request = model_request(
            snapshot,
            config,
            key=result.request_id,
            payload={
                "purpose": "delivery_review" if review_pending(snapshot) else "task",
            },
        )
        result = bind_replay_input(result, request)
    if review_pending(snapshot) and kind == "tool_calls":
        # Admit the surface advertised for this request. Charging its model
        # call must not revoke an already offered source read; the next
        # request uses the updated budget and read-round count.
        readers = {tool.name for tool in delivery_review_readers(snapshot, config)}
        if readers and all(call.name in readers for call in result.tool_calls):
            # Same tool ledger and permissions; only existing host-declared
            # source readers are admitted. Private review prose stays private.
            updated = updated.model_copy(
                update={
                    "delivery_review": snapshot.delivery_review.model_copy(
                        update={
                            "read_rounds": snapshot.delivery_review.read_rounds + 1,
                            "continuation_messages": (),
                        }
                    ),
                }
            )
            sealed = begin_tool_batch(snapshot, updated, result)
            return sealed.model_copy(
                update={
                    "messages": (
                        *sealed.messages[:-1],
                        {**sealed.messages[-1], "kind": "delivery_review_source"},
                    )
                }
            )
    if review_pending(snapshot) and kind not in {
        "provider_retryable_error",
        "policy_abort",
        "user_abort",
        "context_overflow",
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
            return move(declared, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "pending_tools": (),
                    "messages": (
                        *declared.messages,
                        *refused_pending_tools(
                            declared,
                            reason="delivery_read_path_not_written",
                        ),
                    ),
                }
            )
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
                            else "The last response hit its output limit. Return only the exact "
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
