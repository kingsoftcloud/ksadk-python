"""Connect context planning to committed run state, without owning model I/O."""

from __future__ import annotations

import hashlib
import json

from .completion import VerificationReport
from .context import (
    AcceptanceProgress,
    ExecutionContinuityState,
    PlanningProgress,
    VerificationProgress,
)
from .context_contributor_contract import contributor_expired
from .context_fresh_tools import FRESH_TOOL_OBSERVATION_POLICY
from .context_partial_summary import PARTIAL_SUMMARY_POLICY, commit_completed_prefix
from .context_reduction import SUMMARY_REDUCTION_POLICY, replan_summary_projection
from .context_runtime import (
    READER_SUMMARY_POLICY_VERSION,
    CompactionController,
    CompactionHistory,
    CompactionProjectionOverflow,
    CompactionRequest,
    ContextPlanner,
    ContextRuntimeError,
)
from .context_source_progress import source_read_progress
from .context_summary_recovery import SUMMARY_RETRY_POLICY, repartition_failed_summary
from .delivery_review import delivery_review_messages, review_pending, unavailable_review
from .machine import host_resolved_candidate
from .model import ModelTurnResult
from .model_replay import replay_binding_failure
from .operator_guidance import stage_guidance
from .ports import ArtifactRef, RunConfiguration
from .precompact_hook_runtime import (
    planned_precompact_state,
    precompact_feedback_keys,
    precompact_feedback_messages,
)
from .state import CommitBatch, RunPhase, RunSnapshot, move, workspace_observation_cursor
from .stop_hook_runtime import hook_feedback_messages
from .tool_budget import (
    finalization_only,
    model_instructions,
    model_tool_catalog,
    model_turn_control,
)
from .tool_hook_runtime import pending_hook_operation_keys, tool_hook_feedback_messages
from .transitions import commit_transition, effect_operation_key, finish, prepare_effect
from .verification_binding import verification_subject_context_digest


class ContextDriver:
    def __init__(
        self,
        planner: ContextPlanner | None,
        workspace_revision: str,
        *,
        workspace_tracking: bool = False,
        replay_context_policy: str | None = None,
    ):
        if not workspace_revision.strip():
            raise ValueError("workspace revision must be explicit")
        self.planner = planner
        self.workspace_revision = workspace_revision
        self.workspace_tracking = workspace_tracking
        self.binding = (
            hashlib.sha256(
                json.dumps(
                    {
                        "tokenizer": planner.tokenizer_id,
                        "summary_budget_unit": planner.summary_budget_unit,
                        **(
                            {"summary_budget_mode": planner.summary_budget_mode}
                            if planner.summary_budget_mode != "strict"
                            else {}
                        ),
                        "policy": planner.policy.model_dump(mode="json"),
                        "workspace_revision": workspace_revision,
                        "workspace_tracking": workspace_tracking,
                        "request_projection": "ksadk.native.turn-control/v2",
                        "source_capacity_projection": "sealed-artifact-batch/v1",
                        "compaction_admission": "finalization-reserve/v1",
                        "compaction_accounting": "closed-contract-roots/v1",
                        "message_replay_context": "native-anthropic-input-prefix/v2",
                        "fresh_tool_observation": FRESH_TOOL_OBSERVATION_POLICY,
                        "reader_observation_projection": READER_SUMMARY_POLICY_VERSION,
                        "summary_projection_reduction": SUMMARY_REDUCTION_POLICY,
                        "summary_failure_repartition": SUMMARY_RETRY_POLICY,
                        "summary_partial_commit": PARTIAL_SUMMARY_POLICY,
                        **(
                            {"summary_completion_policy": planner.summary_completion_policy}
                            if planner.summary_completion_policy is not None
                            else {}
                        ),
                        **(
                            {"subagent_continuity_policy": planner.subagent_continuity_policy}
                            if planner.subagent_continuity_policy is not None
                            else {}
                        ),
                        **(
                            {"source_progress_policy": planner.source_progress_policy}
                            if planner.source_progress_policy is not None
                            else {}
                        ),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if planner
            else (hashlib.sha256(
                b"native-anthropic-input-prefix/v2|no-planner"
            ).hexdigest() if replay_context_policy is not None else "")
        )

    def continuity(self, snapshot: RunSnapshot, config: RunConfiguration):
        required = tuple(i.acceptance_id for i in config.contract.items if i.required)
        report = (
            VerificationReport.model_validate(snapshot.verification)
            if snapshot.verification is not None
            else None
        )
        cursor = snapshot.verification_tool_calls
        validated = (
            report is not None
            and cursor is not None
            and cursor <= snapshot.tool_calls
            and report.contract_revision == config.contract.revision
        )
        since = snapshot.tool_calls - cursor if validated else None
        current_binding = (
            validated
            and report.subject_context_digest == verification_subject_context_digest(snapshot)
        )
        candidate = snapshot.candidate or {}
        same_candidate = report is not None and all(
            candidate.get(key) == getattr(report, key)
            for key in (
                "candidate_id",
                "subject_digest",
                "subject_context_digest",
                "environment_digest",
            )
        )
        by_id = {item.acceptance_id: item for item in report.items} if report else {}
        progress = []
        for item in config.contract.items:
            previous = by_id.get(item.acceptance_id)
            status = "pending"
            needs_reverification = True
            if current_binding and since == 0 and previous is not None:
                if previous.status in {"failed", "blocked"}:
                    status = previous.status
                elif (
                    previous.status == "passed"
                    and (same_candidate or not candidate)
                    and previous.evidence_refs
                    and previous.verification_method != "model_assisted"
                ):
                    status = "passed"
                    needs_reverification = not same_candidate
            progress.append(
                AcceptanceProgress(
                    acceptance_id=item.acceptance_id,
                    required=item.required,
                    status=status,
                    last_verdict=previous.status if previous else None,
                    needs_reverification=needs_reverification,
                    safe_summary=previous.safe_summary if previous else "",
                    repair_target=(
                        previous.repair_target if current_binding and previous else None
                    ),
                    evidence_refs=previous.evidence_refs if previous else (),
                    verifier_id=previous.verifier_id if previous else None,
                    verifier_version=previous.verifier_version if previous else None,
                    verification_method=(
                        previous.verification_method if previous else item.verification_method
                    ),
                    repairable=(
                        previous.repairable
                        if previous is not None and previous.repairable is not None
                        else report.repairable
                        if previous is not None and report is not None
                        else None
                    ),
                )
            )
        # A passed checkpoint item is finished implementation work on this
        # unchanged subject, but still needs a fresh final-candidate verdict.
        # Pending work is the repair queue, not the final verification set.
        pending = tuple(
            i.acceptance_id
            for i in progress
            if validated
            and i.required
            and i.status != "passed"
            and i.last_verdict in {"failed", "blocked", "not_run"}
            and i.repairable is not False
        )
        pending_verification = tuple(
            i.acceptance_id
            for i in progress
            if i.required and (i.status != "passed" or i.needs_reverification)
        )
        unresolved = snapshot.unresolved_interaction_observation or {}
        unresolved_ref = unresolved.get("evidence_ref")
        unresolved_ref = unresolved_ref if isinstance(unresolved_ref, str) else None
        unresolved_entry = unresolved.get("entry_path")
        # These are host-written transcript metadata, never fields parsed from
        # tool content. Scope/run/goal and the direct-tool cursor constrain refs.
        refs = tuple(
            dict.fromkeys(
                message["receipt_ref"]
                for message in snapshot.messages
                if message.get("role") == "tool"
                and isinstance(message.get("receipt_ref"), str)
                and message["receipt_ref"].startswith(f"{snapshot.run_id}:effect:")
                and message.get("receipt_goal_revision") == snapshot.goal_revision
                and type(message.get("receipt_tool_call")) is int
                and (cursor if validated else 0)
                < message["receipt_tool_call"]
                <= snapshot.tool_calls
            )
        )
        # Compaction retries consume model calls while sealing the same source.
        # Keep its captured control until sealing; ordinary preparation then
        # derives a fresh control from the updated shared budget.
        control = (
            CompactionRequest.model_validate(snapshot.compaction_request).turn_control
            if snapshot.phase == RunPhase.COMPACTING and snapshot.compaction_request
            else model_turn_control(snapshot, config)
        )
        if review_pending(snapshot):
            next_action = (
                "Return the host-requested delivery review JSON from current observations; "
                "review findings are diagnostics and cannot pass acceptance."
            )
        elif snapshot.pending_interaction:
            next_action = "Resolve the pending interaction before continuing its tool execution."
        elif snapshot.pending_tools:
            next_action = "Finish pending tools, then assess the verification observations."
        elif control is not None:
            next_action = control.instruction
        elif unresolved_ref and isinstance(unresolved_entry, str):
            next_action = (
                f"Recheck the failed browser interaction on {unresolved_entry} with an actual "
                "action after repair; a screenshot-only check does not clear the error."
            )
        elif pending:
            next_action = (
                "Inspect the latest verification and tool observations; repair if needed, then "
                f"verify {', '.join(pending)} against a new candidate before claiming completion."
            )
        else:
            next_action = None
        if self.workspace_tracking:
            workspace_revision = snapshot.workspace_revision
            workspace_state = snapshot.workspace_observation_status
            workspace_changed_paths = snapshot.workspace_changed_paths
            workspace_reason = snapshot.workspace_observation_reason
        else:
            workspace_revision = (
                self.workspace_revision
                if self.workspace_revision != "unavailable" and snapshot.tool_calls == 0
                else None
            )
            workspace_state = "initial" if workspace_revision is not None else "unavailable"
            workspace_changed_paths = ()
            workspace_reason = None
        artifacts = tuple(ArtifactRef.model_validate(ref) for ref in snapshot.artifact_refs)
        return ExecutionContinuityState(
            goal=snapshot.goal,
            goal_revision=snapshot.goal_revision,
            pending_hook_operation_keys=(
                *pending_hook_operation_keys(snapshot), *precompact_feedback_keys(snapshot),
            ),
            contract_digest=snapshot.contract_digest,
            required_acceptance_ids=required,
            pending_work=pending,
            pending_verification=pending_verification,
            failure_fingerprints=tuple(
                value for value in (snapshot.failure_fingerprint, unresolved_ref) if value
            ),
            evidence_refs=tuple(
                dict.fromkeys(
                    (
                        *(ref for item in progress for ref in item.evidence_refs),
                        *((unresolved_ref,) if unresolved_ref else ()),
                    )
                )
            ),
            artifact_refs=tuple(ref.uri for ref in artifacts),
            next_action=next_action,
            # Dynamic observations are host effects. Without that port, a
            # pinned initial binding cannot attest to code after tool effects.
            workspace_revision=workspace_revision,
            observed_progress_revision=snapshot.observed_progress_revision,
            workspace_state=workspace_state,
            workspace_changed_paths=workspace_changed_paths,
            workspace_observation_reason=workspace_reason,
            acceptance_progress=tuple(progress),
            last_verification=(
                VerificationProgress(
                    candidate_id=report.candidate_id,
                    contract_revision=report.contract_revision,
                    subject_digest=report.subject_digest,
                    subject_context_digest=report.subject_context_digest,
                    environment_digest=report.environment_digest,
                    binding_validated=current_binding,
                    tool_calls_at_verification=cursor,
                    tool_calls_since_verification=since,
                )
                if report is not None
                else None
            ),
            tool_receipt_refs=refs,
            task_progress=tuple(
                PlanningProgress.model_validate({**item, "authority": "model_planning_only"})
                for item in snapshot.task_progress
            ),
            task_progress_revision=snapshot.task_progress_revision,
            artifact_state="available" if artifacts else "unknown",
            pending_interaction_refs=(
                (snapshot.pending_interaction,) if snapshot.pending_interaction else ()
            ),
            unresolved_observation_refs=(unresolved_ref,) if unresolved_ref else (),
            pending_tool_call_ids=tuple(call["call_id"] for call in snapshot.pending_tools),
            pending_subagent_refs=tuple(dict.fromkeys((
                *(child.child_run_id for child in snapshot.child_budget_reservations
                  if child.settled_usage_digest is None),
                *snapshot.pending_child_observation_ids,
            ))),
            pending_guidance_ids=snapshot.pending_guidance_ids,
            context_source_operation_key=(
                snapshot.context_source_receipt.get("operation_key")
                if snapshot.context_source_receipt is not None
                and snapshot.context_source_receipt.get("model_call_cursor") is not None
                else None
            ),
            accounted_subagent_run_ids=(
                snapshot.child_run_ids
                if self.planner is not None
                and self.planner.subagent_continuity_policy == "accounted_child_runs/v1"
                else ()
            ),
            source_read_progress=(
                source_read_progress(snapshot, config.tools,
                                     max_chars=self.planner.policy.fresh_read_max_chars)
                if self.planner is not None
                and self.planner.source_progress_policy == "receipt-source-progress/v2"
                else ()
            ),
        )

    def prepare(self, snapshot: RunSnapshot, config: RunConfiguration) -> CommitBatch:
        if snapshot.model_budget_claimed >= config.limits.max_model_calls:
            return commit_transition(
                snapshot,
                finish(snapshot, "budget_exhausted", "model_budget_exhausted"),
                "budget.exhausted",
            )
        rich = [source for source in config.context_sources
                if source.contributor_policy is not None]
        source_cursor = (snapshot.context_source_receipt or {}).get("model_call_cursor")
        expired = contributor_expired(snapshot.context_source_receipt) if rich else False
        refresh = (snapshot.context_source_goal_revision != snapshot.goal_revision
                   or (rich and source_cursor != snapshot.model_calls and (
                       expired or any(source.contributor_policy.capabilities.cacheability == "none"
                                      for source in rich)
                   )))
        if config.context_sources and refresh:
            payload = {"goal_revision": snapshot.goal_revision}
            if rich:
                try:
                    payload["input_budget"] = self.planner.contributor_budget(
                        profile=config.profile, instructions=model_instructions(snapshot, config),
                        tools=model_tool_catalog(snapshot, config), messages=snapshot.messages,
                        continuity=self.continuity(snapshot, config).model_copy(update={
                            "context_source_operation_key": effect_operation_key(snapshot),
                        }), contract=config.contract,
                        history=CompactionHistory.model_validate(snapshot.compaction_history),
                        turn_control=model_turn_control(snapshot, config),
                    )
                except (ValueError, TypeError):
                    return commit_transition(snapshot,
                                             finish(snapshot, "blocked", "context_counter_invalid"),
                                             "context.blocked")
            return prepare_effect(snapshot, "context", payload)
        if expired:
            return commit_transition(snapshot,
                                     finish(snapshot, "blocked", "context_source_expired"),
                                     "context.blocked")
        cursor = workspace_observation_cursor(snapshot, "model")
        if self.workspace_tracking and snapshot.workspace_observation_cursor != cursor:
            return prepare_effect(snapshot, "workspace", {"cursor": cursor})
        if resolved := host_resolved_candidate(snapshot, config):
            return resolved
        reserve = config.limits.verification_reserve_model_calls
        idle = config.limits.verification_idle_tool_calls
        budget_boundary = bool(
            reserve
            and snapshot.model_budget_claimed
            >= config.limits.max_model_calls - reserve
        )
        idle_boundary = bool(
            idle
            and snapshot.last_workspace_mutation_tool_call is not None
            and snapshot.tool_calls - snapshot.last_workspace_mutation_tool_call >= idle
        )
        browser_boundary = bool(
            config.workspace_observation_policy is not None
            and snapshot.last_web_mutation_tool_call is not None
            and snapshot.last_web_mutation_tool_call
            > (snapshot.checkpoint_web_mutation_tool_call or 0)
            and isinstance(snapshot.last_browser_observation, dict)
            and snapshot.last_browser_observation.get("tool_call") == snapshot.tool_calls
            and snapshot.tool_calls >= snapshot.last_web_mutation_tool_call
        )
        if (
            not review_pending(snapshot)
            and (budget_boundary or idle_boundary or browser_boundary)
            and self.workspace_tracking
            and snapshot.tool_calls
            and snapshot.workspace_revision is not None
            and snapshot.workspace_dirty_since_checkpoint
            and snapshot.workspace_revision != snapshot.checkpoint_workspace_revision
        ):
            candidate = {
                "candidate_id": (
                    f"{snapshot.run_id}:checkpoint:{snapshot.model_calls}:{snapshot.tool_calls}"
                ),
                "final_text": "",
                "purpose": "checkpoint",
            }
            updated = move(snapshot, RunPhase.VERIFYING).model_copy(
                update={"candidate": candidate, "prepared_context": None}
            )
            return commit_transition(
                snapshot,
                updated,
                "verification.checkpoint_started",
                {"workspace_revision": snapshot.workspace_revision},
            )
        if config.profile.operator_guidance_policy == "journaled-system/v1":
            ledger = ContextPlanner._host_ledger(self.continuity(snapshot, config), config.contract)
            base_messages = delivery_review_messages(snapshot)
            projected_messages = precompact_feedback_messages(snapshot, tool_hook_feedback_messages(
                snapshot, hook_feedback_messages(snapshot, base_messages),
            ))
            additions = stage_guidance(
                projected_messages, profile=config.profile,
                run_id=snapshot.run_id, goal_revision=snapshot.goal_revision,
                model_call=snapshot.model_calls, base_instructions=config.instructions,
                instructions=model_instructions(snapshot, config), ledger=ledger,
                control=model_turn_control(snapshot, config),
                purpose="delivery_review" if review_pending(snapshot) else "task",
            )
            if additions:
                # Persist admitted Hook observations before the guidance that
                # follows them. Their original producer checks and provenance
                # remain authoritative; a system notice never absorbs raw data.
                additions = (*projected_messages[len(base_messages):], *additions)
                updates = ({"delivery_review": snapshot.delivery_review.model_copy(update={
                    "continuation_messages": (
                        *snapshot.delivery_review.continuation_messages, *additions,
                    ),
                })} if review_pending(snapshot) else {"messages": (*snapshot.messages, *additions)})
                return commit_transition(snapshot, snapshot.model_copy(update=updates),
                                         "model.guidance_staged", {
                                             "guidance_key": additions[-1]["guidance_key"],
                                             "policy": config.profile.operator_guidance_policy,
                                         })
        if self.planner is None:
            return commit_transition(
                snapshot, move(snapshot, RunPhase.REQUESTING_MODEL), "run.phase_changed"
            )
        plan = self.planner.plan(
            source_revision=snapshot.revision,
            model_turn=snapshot.model_calls,
            profile=config.profile,
            instructions=(config.instructions
                          if config.profile.operator_guidance_policy == "journaled-system/v1"
                          else model_instructions(snapshot, config)),
            tools=model_tool_catalog(snapshot, config),
            context_tools=config.tools,
            messages=precompact_feedback_messages(snapshot, tool_hook_feedback_messages(
                snapshot, hook_feedback_messages(snapshot, delivery_review_messages(snapshot))
            )),
            continuity=self.continuity(snapshot, config),
            contract=config.contract,
            history=CompactionHistory.model_validate(snapshot.compaction_history),
            force_compaction=snapshot.force_compaction,
            turn_control=model_turn_control(snapshot, config),
            allow_compaction=not finalization_only(snapshot, config),
        )
        if review_pending(snapshot) and plan.action in {"blocked", "compact"}:
            # Never compact a review projection into the implementation
            # transcript. An oversized diagnostic is unavailable, not passed.
            reason = "review_context_budget_unavailable"
            return commit_transition(
                snapshot, unavailable_review(snapshot, reason),
                "completion.delivery_review_unavailable", {"reason": reason},
            )
        if plan.action == "blocked":
            return commit_transition(
                snapshot,
                finish(snapshot, "blocked", plan.reason),
                "context.blocked",
                {"reason": plan.reason},
            )
        if plan.action == "compact":
            hook_state = planned_precompact_state(snapshot, config, plan.request)
            retained_refs = set(plan.request.retained_message_refs)
            retained_messages = tuple(
                item for item in plan.request.old_messages if item.message_id in retained_refs
            )
            updated = move(snapshot, RunPhase.COMPACTING).model_copy(
                update={
                    "compaction_request": plan.request.model_dump(mode="json"),
                    "prepared_context": None,
                    "precompact_hook_state": hook_state,
                }
            )
            return commit_transition(
                snapshot,
                updated,
                "context.precompact_started" if hook_state and hook_state["next_hook_index"] == 0
                else "context.compaction_started",
                {
                    "source_revision": snapshot.revision,
                    "source_message_count": len(plan.request.old_messages),
                    "retained_message_count": len(retained_messages),
                    "retained_tool_result_count": sum(
                        item.role == "tool" for item in retained_messages
                    ),
                    "retained_input_tokens": sum(item.token_count for item in retained_messages),
                    "summary_input_tokens": plan.request.summary_counted_input_tokens,
                    "window_tokens": plan.request.budget.window_tokens,
                    "fixed_tokens": plan.request.budget.fixed_tokens,
                    "max_output_tokens": plan.request.budget.max_output_tokens,
                    "input_capacity": plan.request.budget.input_capacity,
                    "target_ratio": self.planner.policy.target_ratio,
                    "trigger_ratio": self.planner.policy.trigger_ratio,
                },
            )
        updated = move(snapshot, RunPhase.REQUESTING_MODEL).model_copy(
            update={
                "prepared_context": plan.prepared.model_dump(mode="json"),
                "compaction_request": None,
                "compaction_history": (
                    {**snapshot.compaction_history,
                     "abandoned_replay_digests": list(plan.prepared.abandoned_replay_digests)}
                    if plan.prepared.abandoned_replay_digests else snapshot.compaction_history
                ),
            }
        )
        return commit_transition(
            snapshot,
            updated,
            "context.prepared",
            {
                "counted_input_tokens": plan.prepared.counted_input_tokens,
                "tokenizer_id": plan.prepared.tokenizer_id,
                **({"tool_result_pruning":
                    plan.prepared.tool_result_pruning.model_dump(mode="json")}
                   if plan.prepared.tool_result_pruning is not None else {}),
            },
        )

    def prepare_verification(self, snapshot: RunSnapshot) -> CommitBatch | None:
        if not self.workspace_tracking:
            return None
        cursor = workspace_observation_cursor(snapshot, "verification")
        if snapshot.workspace_observation_cursor == cursor:
            return None
        return prepare_effect(
            snapshot,
            "workspace",
            {"cursor": cursor, "purpose": "verification"},
        )

    def begin_summary(self, snapshot: RunSnapshot, config: RunConfiguration) -> CommitBatch:
        if self.planner is None or snapshot.compaction_request is None:
            return commit_transition(
                snapshot,
                finish(snapshot, "blocked", "context_compaction_unavailable"),
                "context.blocked",
            )
        if snapshot.model_budget_claimed >= config.limits.max_model_calls:
            return commit_transition(
                snapshot,
                finish(snapshot, "budget_exhausted", "model_budget_exhausted"),
                "budget.exhausted",
            )
        if finalization_only(snapshot, config):
            try:
                compacted = commit_completed_prefix(
                    CompactionController(self.planner),
                    CompactionRequest.model_validate(snapshot.compaction_request),
                    current_revision=snapshot.revision,
                    current_continuity=self.continuity(snapshot, config),
                    current_messages=snapshot.messages,
                )
            except (ContextRuntimeError, ValueError):
                compacted = None
            if compacted is not None:
                return commit_transition(
                    snapshot,
                    move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(update={
                        "compaction_history": compacted.history.model_dump(mode="json"),
                        "compaction_request": None, "prepared_context": None,
                        "force_compaction": False, "model_errors": 0, "provider_errors": 0,
                    }),
                    "context.compaction_partial_sealed",
                    {"reason": "model_budget_reserved", "policy": PARTIAL_SUMMARY_POLICY,
                     "retained_message_count": len(compacted.history.retained_message_refs)},
                )
            # A retry/continuation or restored checkpoint can reach the
            # reserve after compaction began. Abandon only the unsealed
            # projection and replan from committed history for delivery.
            return commit_transition(
                snapshot,
                move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(update={
                    "compaction_request": None,
                    "prepared_context": None,
                    "model_errors": 0,
                    "provider_errors": 0,
                }),
                "context.compaction_deferred",
                {"reason": "model_budget_reserved"},
            )
        if snapshot.compaction_request.get("fresh_tool_observation_policy") is not None:
            try:
                request = CompactionRequest.model_validate(snapshot.compaction_request)
                CompactionController(self.planner)._validate_source(
                    request, snapshot.revision, self.continuity(snapshot, config), snapshot.messages
                )
            except (ContextRuntimeError, ValueError) as error:
                reason = str(error) if isinstance(error, ContextRuntimeError) else (
                    "context_compaction_request_invalid"
                )
                return commit_transition(
                    snapshot, finish(snapshot, "blocked", reason),
                    "context.blocked", {"reason": reason},
                )
        return prepare_effect(snapshot, "model", {"purpose": "compaction"})

    def seal(self, snapshot: RunSnapshot, result: ModelTurnResult, config: RunConfiguration):
        from .model_input import input_binding_failure, observe_input

        provider_error = result.response_kind == "provider_retryable_error"
        updated = snapshot.model_copy(
            update={
                "pending_intent": None,
                "model_calls": snapshot.model_calls + 1,
                "provider_errors": snapshot.provider_errors + 1 if provider_error else 0,
            }
        )
        if failure := input_binding_failure(config.profile, result.input_receipt):
            return finish(updated, "failed", failure)
        updated = observe_input(updated, result.input_receipt)
        if failure := replay_binding_failure(
            config.profile, result.provider_replay,
            required=result.response_kind in {"tool_calls", "final_candidate", "output_limit"},
        ):
            return finish(updated, "failed", failure)
        bounded_summary = (
            self.planner is not None
            and self.planner.summary_completion_policy == "bounded-committed-summary/v1"
            and result.response_kind == "output_limit"
            and bool(result.text.strip())
        )
        if bounded_summary and result.text_commit_version != "provider-finished-text/v1":
            return finish(updated, "blocked", "context_summary_provider_commit_missing")
        if (result.response_kind == "output_limit" and result.text.strip()
                and self.planner and not bounded_summary):
            try:
                request = CompactionRequest.model_validate(snapshot.compaction_request)
                continued = CompactionController(self.planner).continue_summary(
                    request,
                    result.text,
                    current_revision=snapshot.revision,
                    current_continuity=self.continuity(snapshot, config),
                    current_messages=snapshot.messages,
                    provider_replay=result.provider_replay,
                )
            except (ContextRuntimeError, ValueError) as exc:
                return finish(
                    updated,
                    "blocked",
                    str(exc)
                    if isinstance(exc, ContextRuntimeError)
                    else "compaction_seal_rejected",
                )
            return updated.model_copy(
                update={
                    "model_errors": 0,
                    "provider_errors": 0,
                    "compaction_request": continued.model_dump(mode="json"),
                }
            )
        if result.response_kind in {
            "provider_retryable_error",
            "empty_response",
            "malformed_tool_call",
            "output_limit",
            "context_overflow",
        } and not bounded_summary:
            errors = updated.provider_errors if provider_error else snapshot.model_errors + 1
            updated = updated.model_copy(update={"model_errors": 0 if provider_error else errors})
            limit = (
                config.profile.max_provider_retries
                if provider_error
                else config.profile.max_retries
            )
            if errors <= limit:
                if result.response_kind in {"output_limit", "context_overflow"}:
                    if self.planner is None:
                        return finish(updated, "blocked", "compaction_summary_incomplete")
                    try:
                        retried = repartition_failed_summary(
                            self.planner,
                            CompactionRequest.model_validate(snapshot.compaction_request),
                            current_revision=snapshot.revision,
                            current_continuity=self.continuity(snapshot, config),
                            current_messages=snapshot.messages,
                        )
                    except (ContextRuntimeError, ValueError) as exc:
                        return finish(
                            updated, "blocked", str(exc)
                            if isinstance(exc, ContextRuntimeError) else "compaction_seal_rejected",
                        )
                    if retried is not None:
                        return updated.model_copy(update={
                            "compaction_request": retried.model_dump(mode="json"),
                        })
                    if result.response_kind == "context_overflow":
                        return finish(updated, "blocked", "compaction_summary_incomplete")
                # No summary has been sealed, so retain the exact compaction
                # request and retry with a fresh model effect/operation key.
                return updated
            return finish(updated, "blocked", "compaction_summary_retry_exhausted")
        if ((result.response_kind != "final_candidate" and not bounded_summary)
                or self.planner is None):
            return finish(updated, "blocked", "compaction_summary_incomplete")
        updated = updated.model_copy(update={"model_errors": 0, "provider_errors": 0})
        try:
            request = CompactionRequest.model_validate(snapshot.compaction_request)
            if bounded_summary:
                # This is a lossy, untrusted memory projection, never a final
                # task candidate. Source coverage and window validation below
                # still apply, and this receipt consumed one shared model call.
                request = request.model_copy(update={"summary_output_limited": True})
            compacted = CompactionController(self.planner).advance(
                request,
                result.text,
                current_revision=snapshot.revision,
                current_continuity=self.continuity(snapshot, config),
                current_messages=snapshot.messages,
            )
        except ContextRuntimeError as exc:
            if isinstance(exc, CompactionProjectionOverflow):
                reduced = replan_summary_projection(
                    self.planner, snapshot, updated, config, self.continuity(snapshot, config)
                )
                if reduced is not None:
                    return reduced
            if str(exc) == "context_summary_budget_exceeded":
                errors = snapshot.model_errors + 1
                if errors <= config.profile.max_retries:
                    feedback = (
                        "\nThe previous summary exceeded the hard output limit. "
                        f"Keep the next summary below {request.summary_max_tokens} "
                        f"{self.planner.summary_budget_unit}; use much shorter wording."
                    )
                    retried = request.model_copy(
                        update={
                            "summary_instructions": request.summary_instructions + feedback,
                            # The hard summary ceiling is counted by the host.
                            # Reducing a provider token cap can truncate hidden
                            # reasoning without making the visible text shorter.
                            "summary_output_tokens": request.summary_output_tokens,
                            "summary_messages": (
                                request.summary_messages[: -2 * request.summary_continuations]
                                if request.summary_continuations
                                else request.summary_messages
                            ),
                            "summary_fragments": (),
                            "summary_continuations": 0,
                            "summary_output_limited": snapshot.compaction_request.get(
                                "summary_output_limited", False
                            ),
                        }
                    )
                    return updated.model_copy(
                        update={
                            "model_errors": errors,
                            "compaction_request": retried.model_dump(mode="json"),
                        }
                    )
            return finish(updated, "blocked", str(exc))
        except ValueError:
            return finish(updated, "blocked", "compaction_seal_rejected")
        if isinstance(compacted, CompactionRequest):
            return updated.model_copy(
                update={"compaction_request": compacted.model_dump(mode="json")}
            )
        return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
            update={
                "compaction_history": compacted.history.model_dump(mode="json"),
                "compaction_request": None,
                "prepared_context": None,
                "force_compaction": False,
                # A sealed rewrite starts a different request root. Recovery
                # attempts on the previous root are no longer consecutive;
                # shared model/compaction/stagnation budgets still apply.
                "context_overflows": 0,
            }
        )
